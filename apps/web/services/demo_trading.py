"""Demo trading portfolio service backed by BQuant agent recommendations."""

from __future__ import annotations

import csv
import json
import math
import uuid
from datetime import date, datetime, time, timedelta
from io import StringIO
from typing import Any

import pandas as pd

from agents.orchestrator import run_agent_cycle
from pipelines.live_update_runtime import is_trading_day, market_timezone, now_local
from utils.logger import BQuantLogger
from warehouse.duckdb_connection import get_connection


DEFAULT_ACCOUNT_ID = "demo_tplus_50m"
DEFAULT_ACCOUNT_NAME = "BQuant Demo Trading T+2.5"
INITIAL_CASH = 50_000_000.0
BOARD_LOT = 100
BUY_FEE_RATE = 0.0015
SELL_FEE_RATE = 0.0015
SELL_TAX_RATE = 0.001
SETTLEMENT_CUTOFF = time(hour=13, minute=0)
PRICE_VND_MULTIPLIER = 1000.0
PRICE_ALREADY_VND_THRESHOLD = 1000.0
LOGGER = BQuantLogger("web_demo_trading", component="web", subcomponent="demo_trading", default_channel="web")


def ensure_demo_trading_tables() -> None:
    """Create demo trading tables and the default account when needed.

    Side Effects:
        Executes idempotent DuckDB DDL and inserts the default 50,000,000 VND
        demo account if it does not already exist.
    """
    with get_connection(read_only=False) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS demo_trading_accounts (
                account_id VARCHAR PRIMARY KEY,
                account_name VARCHAR NOT NULL,
                currency VARCHAR NOT NULL DEFAULT 'VND',
                initial_cash DOUBLE NOT NULL,
                cash_balance DOUBLE NOT NULL,
                settlement_rule VARCHAR NOT NULL DEFAULT 'T+2.5',
                board_lot INTEGER NOT NULL DEFAULT 100,
                started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS demo_trading_orders (
                order_id VARCHAR PRIMARY KEY,
                account_id VARCHAR NOT NULL,
                action VARCHAR NOT NULL,
                symbol VARCHAR NOT NULL,
                trade_date DATE NOT NULL,
                trade_time TIMESTAMP NOT NULL,
                price DOUBLE NOT NULL,
                quantity BIGINT NOT NULL,
                gross_amount DOUBLE NOT NULL,
                fees DOUBLE NOT NULL DEFAULT 0,
                taxes DOUBLE NOT NULL DEFAULT 0,
                net_amount DOUBLE NOT NULL,
                realized_pnl DOUBLE NOT NULL DEFAULT 0,
                status VARCHAR NOT NULL,
                settlement_date DATE,
                settlement_ts TIMESTAMP,
                source VARCHAR NOT NULL DEFAULT 'manual',
                source_run_id VARCHAR,
                recommendation VARCHAR,
                rationale_json VARCHAR,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_demo_trading_orders_account_time
            ON demo_trading_orders(account_id, trade_time)
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS demo_trading_lots (
                lot_id VARCHAR PRIMARY KEY,
                account_id VARCHAR NOT NULL,
                symbol VARCHAR NOT NULL,
                buy_order_id VARCHAR NOT NULL,
                trade_date DATE NOT NULL,
                settlement_date DATE NOT NULL,
                settlement_ts TIMESTAMP NOT NULL,
                quantity BIGINT NOT NULL,
                remaining_quantity BIGINT NOT NULL,
                avg_cost DOUBLE NOT NULL,
                status VARCHAR NOT NULL DEFAULT 'open',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_demo_trading_lots_account_symbol
            ON demo_trading_lots(account_id, symbol)
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS demo_trading_daily_plans (
                plan_id VARCHAR PRIMARY KEY,
                account_id VARCHAR NOT NULL,
                plan_date DATE NOT NULL,
                source_run_id VARCHAR,
                source_as_of_date DATE,
                status VARCHAR NOT NULL DEFAULT 'active',
                summary_json VARCHAR NOT NULL,
                generated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (account_id, plan_date)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS demo_trading_plan_items (
                plan_id VARCHAR NOT NULL,
                symbol VARCHAR NOT NULL,
                proposed_action VARCHAR NOT NULL,
                recommendation VARCHAR NOT NULL,
                score DOUBLE NOT NULL DEFAULT 0,
                confidence DOUBLE NOT NULL DEFAULT 0,
                risk_level VARCHAR NOT NULL DEFAULT 'unknown',
                suggested_weight DOUBLE NOT NULL DEFAULT 0,
                current_quantity BIGINT NOT NULL DEFAULT 0,
                sellable_quantity BIGINT NOT NULL DEFAULT 0,
                latest_price DOUBLE,
                current_value DOUBLE NOT NULL DEFAULT 0,
                target_value DOUBLE NOT NULL DEFAULT 0,
                suggested_quantity BIGINT NOT NULL DEFAULT 0,
                rationale_json VARCHAR NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (plan_id, symbol)
            )
            """
        )
        conn.execute(
            """
            INSERT INTO demo_trading_accounts (
                account_id,
                account_name,
                currency,
                initial_cash,
                cash_balance,
                settlement_rule,
                board_lot
            )
            VALUES (?, ?, 'VND', ?, ?, 'T+2.5', ?)
            ON CONFLICT (account_id) DO NOTHING
            """,
            [DEFAULT_ACCOUNT_ID, DEFAULT_ACCOUNT_NAME, INITIAL_CASH, INITIAL_CASH, BOARD_LOT],
        )


def _today() -> date:
    """Return the local market date."""
    return now_local().date()


def _previous_trading_day(anchor: date, *, inclusive: bool = False) -> date:
    """Resolve the previous trading day using BQuant's live-update calendar."""
    current = anchor if inclusive else anchor - timedelta(days=1)
    while not is_trading_day(current):
        current -= timedelta(days=1)
    return current


def _add_trading_days(anchor: date, trading_days: int) -> date:
    """Advance by a number of configured trading days."""
    current = anchor
    remaining = int(trading_days)
    while remaining > 0:
        current += timedelta(days=1)
        if is_trading_day(current):
            remaining -= 1
    return current


def _settlement_timestamp(trade_date: date) -> datetime:
    """Return the T+2.5 settlement timestamp for a trade date."""
    settlement_date = _add_trading_days(trade_date, 2)
    return datetime.combine(settlement_date, SETTLEMENT_CUTOFF).replace(tzinfo=market_timezone()).replace(tzinfo=None)


def _floor_board_lot(quantity: float | int, board_lot: int = BOARD_LOT) -> int:
    """Round a quantity down to the nearest board-lot multiple."""
    if quantity <= 0:
        return 0
    return int(math.floor(float(quantity) / board_lot) * board_lot)


def _safe_float(value: Any, default: float = 0.0) -> float:
    """Convert a nullable value to a finite float."""
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


def _to_vnd_price(value: Any) -> float:
    """Normalize Vietnamese stock quotes to VND per share.

    BQuant chart data stores HOSE stock prices in the market convention of
    thousands of VND. Demo Trading cash is modeled in VND, so quote values below
    1,000 are treated as thousands and multiplied by 1,000. User-imported
    prices like `115000` are already VND and pass through unchanged.
    """
    price = _safe_float(value)
    if price <= 0:
        return 0.0
    return price * PRICE_VND_MULTIPLIER if price < PRICE_ALREADY_VND_THRESHOLD else price


def _latest_price_map(as_of_date: date | None = None) -> dict[str, dict[str, Any]]:
    """Load latest stock prices on or before a date.

    Args:
        as_of_date: Optional trading-date ceiling. Defaults to the latest row
            available for each symbol.

    Returns:
        Mapping from ticker to latest close/date metadata.
    """
    ensure_demo_trading_tables()
    clause = "WHERE trading_date <= ?" if as_of_date else ""
    params: list[Any] = [as_of_date] if as_of_date else []
    with get_connection(read_only=True) as conn:
        rows = conn.execute(
            f"""
            WITH ranked AS (
                SELECT symbol,
                       trading_date,
                       close,
                       row_number() OVER (PARTITION BY symbol ORDER BY trading_date DESC) AS rn
                FROM daily_ohlcv_base
                {clause}
            )
            SELECT symbol, trading_date, close
            FROM ranked
            WHERE rn = 1
            """,
            params,
        ).fetchall()
    return {
        str(symbol): {"trading_date": pd.Timestamp(trading_date).date(), "close": _to_vnd_price(close)}
        for symbol, trading_date, close in rows
    }


def _load_account() -> dict[str, Any]:
    """Load the default demo account row."""
    ensure_demo_trading_tables()
    with get_connection(read_only=True) as conn:
        row = conn.execute(
            """
            SELECT account_id, account_name, currency, initial_cash, cash_balance, settlement_rule, board_lot, started_at
            FROM demo_trading_accounts
            WHERE account_id = ?
            """,
            [DEFAULT_ACCOUNT_ID],
        ).fetchone()
    if not row:
        raise RuntimeError("Demo trading account is not initialized")
    return {
        "account_id": row[0],
        "account_name": row[1],
        "currency": row[2],
        "initial_cash": _safe_float(row[3]),
        "cash_balance": _safe_float(row[4]),
        "settlement_rule": row[5],
        "board_lot": int(row[6]),
        "started_at": str(row[7]),
    }


def _load_positions(moment: datetime | None = None) -> list[dict[str, Any]]:
    """Load current open lots aggregated by symbol."""
    current = (moment or now_local()).replace(tzinfo=None)
    ensure_demo_trading_tables()
    with get_connection(read_only=True) as conn:
        rows = conn.execute(
            """
            SELECT symbol,
                   sum(remaining_quantity) AS quantity,
                   sum(CASE WHEN settlement_ts <= ? THEN remaining_quantity ELSE 0 END) AS sellable_quantity,
                   sum(remaining_quantity * avg_cost) / nullif(sum(remaining_quantity), 0) AS avg_cost,
                   sum(remaining_quantity * avg_cost) AS cost_basis,
                   min(settlement_ts) AS first_settlement_ts,
                   max(settlement_ts) AS last_settlement_ts
            FROM demo_trading_lots
            WHERE account_id = ?
              AND remaining_quantity > 0
              AND status = 'open'
            GROUP BY symbol
            ORDER BY symbol
            """,
            [current, DEFAULT_ACCOUNT_ID],
        ).fetchall()
    return [
        {
            "symbol": row[0],
            "quantity": int(row[1] or 0),
            "sellable_quantity": int(row[2] or 0),
            "avg_cost": _safe_float(row[3]),
            "cost_basis": _safe_float(row[4]),
            "first_settlement_ts": str(row[5]) if row[5] else "",
            "last_settlement_ts": str(row[6]) if row[6] else "",
        }
        for row in rows
    ]


def _latest_recommendations(max_as_of_date: date | None = None) -> pd.DataFrame:
    """Load latest agent recommendations with run metadata.

    Args:
        max_as_of_date: Optional date ceiling. Demo Trading uses the previous
            trading day so morning plans do not rely on same-day bars.

    Returns:
        Recommendation frame for the latest successful run within the date
        ceiling.
    """
    ensure_demo_trading_tables()
    date_clause = "AND runs.as_of_date <= ?" if max_as_of_date else ""
    params: list[Any] = [max_as_of_date] if max_as_of_date else []
    with get_connection(read_only=True) as conn:
        return conn.execute(
            f"""
            WITH latest_run AS (
                SELECT run_id, as_of_date, created_at
                FROM agent_recommendation_runs runs
                WHERE status = 'success'
                {date_clause}
                ORDER BY as_of_date DESC, created_at DESC
                LIMIT 1
            )
            SELECT rec.run_id,
                   rec.as_of_date,
                   rec.symbol,
                   rec.recommendation,
                   rec.confidence,
                   rec.score,
                   rec.risk_level,
                   rec.suggested_weight,
                   rec.rationale_json,
                   runs.created_at AS run_created_at
            FROM agent_recommendations rec
            INNER JOIN latest_run runs
              ON rec.run_id = runs.run_id
            ORDER BY rec.suggested_weight DESC, rec.score DESC, rec.confidence DESC, rec.symbol
            """,
            params,
        ).df()


def import_portfolio(csv_text: str, *, cash_balance: float | None = None) -> dict[str, Any]:
    """Replace the demo portfolio with user-provided current holdings.

    Args:
        csv_text: CSV text with `symbol,quantity,avg_cost` columns. A
            headerless three-column form is also accepted.
        cash_balance: Optional cash balance after import. When omitted, cash is
            computed as `50,000,000 - imported cost basis`, floored at zero.

    Returns:
        Import summary with row count, imported value, and cash balance.

    Side Effects:
        Deletes existing lots/orders/plans for the demo account, creates
        settled import lots, and updates account cash. This makes the imported
        holdings the new demo starting point.
    """
    ensure_demo_trading_tables()
    rows = _parse_portfolio_csv(csv_text)
    if not rows:
        raise ValueError("No valid portfolio rows found. Use symbol,quantity,avg_cost.")

    imported_value = sum(row["quantity"] * row["avg_cost"] for row in rows)
    resolved_cash = float(cash_balance) if cash_balance is not None else max(0.0, INITIAL_CASH - imported_value)
    current = now_local().replace(tzinfo=None)
    trade_date = _today()
    settlement_ts = current - timedelta(minutes=1)
    settlement_date = settlement_ts.date()

    with get_connection(read_only=False) as conn:
        for table in ["demo_trading_plan_items", "demo_trading_daily_plans", "demo_trading_lots", "demo_trading_orders"]:
            key = "plan_id IN (SELECT plan_id FROM demo_trading_daily_plans WHERE account_id = ?)" if table == "demo_trading_plan_items" else "account_id = ?"
            conn.execute(f"DELETE FROM {table} WHERE {key}", [DEFAULT_ACCOUNT_ID])

        for row in rows:
            order_id = str(uuid.uuid4())
            lot_id = str(uuid.uuid4())
            gross = row["quantity"] * row["avg_cost"]
            conn.execute(
                """
                INSERT INTO demo_trading_orders (
                    order_id, account_id, action, symbol, trade_date, trade_time,
                    price, quantity, gross_amount, fees, taxes, net_amount,
                    realized_pnl, status, settlement_date, settlement_ts, source,
                    source_run_id, recommendation, rationale_json
                )
                VALUES (?, ?, 'IMPORT', ?, ?, ?, ?, ?, ?, 0, 0, ?, 0, 'filled', ?, ?, 'user_import', NULL, 'imported_position', ?)
                """,
                [
                    order_id,
                    DEFAULT_ACCOUNT_ID,
                    row["symbol"],
                    trade_date,
                    current,
                    row["avg_cost"],
                    row["quantity"],
                    gross,
                    gross,
                    settlement_date,
                    settlement_ts,
                    json.dumps({"imported": True, "source": "user_csv"}, sort_keys=True),
                ],
            )
            conn.execute(
                """
                INSERT INTO demo_trading_lots (
                    lot_id, account_id, symbol, buy_order_id, trade_date,
                    settlement_date, settlement_ts, quantity, remaining_quantity,
                    avg_cost, status
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open')
                """,
                [
                    lot_id,
                    DEFAULT_ACCOUNT_ID,
                    row["symbol"],
                    order_id,
                    trade_date,
                    settlement_date,
                    settlement_ts,
                    row["quantity"],
                    row["quantity"],
                    row["avg_cost"],
                ],
            )
        conn.execute(
            """
            UPDATE demo_trading_accounts
            SET cash_balance = ?, updated_at = CURRENT_TIMESTAMP
            WHERE account_id = ?
            """,
            [resolved_cash, DEFAULT_ACCOUNT_ID],
        )

    LOGGER.log_web_event(
        "Imported demo trading portfolio",
        event_type="demo_trading_import",
        status="success",
        row_count=len(rows),
        imported_value=round(imported_value, 2),
        cash_balance=round(resolved_cash, 2),
    )
    return {
        "imported_rows": len(rows),
        "imported_value": imported_value,
        "cash_balance": resolved_cash,
    }


def _parse_portfolio_csv(csv_text: str) -> list[dict[str, Any]]:
    """Parse user portfolio CSV text into normalized holding rows."""
    text = (csv_text or "").strip()
    if not text:
        return []
    sample = text.splitlines()[0].lower()
    has_header = "symbol" in sample and "quantity" in sample
    handle = StringIO(text)
    if has_header:
        reader = csv.DictReader(handle)
    else:
        reader = csv.DictReader(handle, fieldnames=["symbol", "quantity", "avg_cost"])

    rows: list[dict[str, Any]] = []
    for raw in reader:
        symbol = str(raw.get("symbol") or raw.get("ticker") or "").strip().upper()
        quantity = int(_safe_float(raw.get("quantity") or raw.get("qty"), 0.0))
        avg_cost = _to_vnd_price(raw.get("avg_cost") or raw.get("cost") or raw.get("price"))
        if not symbol or quantity <= 0 or avg_cost <= 0:
            continue
        rows.append(
            {
                "symbol": symbol,
                "quantity": _floor_board_lot(quantity, BOARD_LOT),
                "avg_cost": avg_cost,
            }
        )
    return [row for row in rows if row["quantity"] > 0]


def generate_daily_plan(*, force: bool = False, refresh_recommendations: bool = False) -> dict[str, Any]:
    """Generate the current trading-day action plan from latest agents.

    Args:
        force: Whether to replace today's existing plan.
        refresh_recommendations: Whether to run the deterministic agent cycle
            before building the plan.

    Returns:
        Daily plan summary and item count.

    Side Effects:
        Optionally runs the recommendation agent cycle and writes plan rows to
        `demo_trading_daily_plans` and `demo_trading_plan_items`.
    """
    ensure_demo_trading_tables()
    plan_date = _today()
    source_expected_date = _previous_trading_day(plan_date, inclusive=False)
    if refresh_recommendations:
        run_agent_cycle(as_of_date=source_expected_date, trigger_type="manual")

    existing = _current_plan_row(plan_date)
    if existing and not force:
        return {"plan_id": existing["plan_id"], "status": "exists", "item_count": existing["item_count"]}
    if existing and force:
        with get_connection(read_only=False) as conn:
            conn.execute("DELETE FROM demo_trading_plan_items WHERE plan_id = ?", [existing["plan_id"]])
            conn.execute("DELETE FROM demo_trading_daily_plans WHERE plan_id = ?", [existing["plan_id"]])

    recommendations = _latest_recommendations(max_as_of_date=source_expected_date)
    if recommendations.empty:
        raise RuntimeError(
            f"No agent recommendations available on or before {source_expected_date}. "
            "Run Refresh Agents + Plan first."
        )

    account = _load_account()
    positions = {row["symbol"]: row for row in _load_positions()}
    prices = _latest_price_map(pd.Timestamp(recommendations["as_of_date"].max()).date())
    equity = _portfolio_equity(account, list(positions.values()), prices)
    cash_balance = _safe_float(account["cash_balance"])
    planning_cash_balance = cash_balance
    plan_id = str(uuid.uuid4())
    rows: list[dict[str, Any]] = []

    for _, rec in recommendations.iterrows():
        symbol = str(rec["symbol"]).upper()
        price = _safe_float(prices.get(symbol, {}).get("close"))
        position = positions.get(symbol, {})
        current_quantity = int(position.get("quantity", 0) or 0)
        sellable_quantity = int(position.get("sellable_quantity", 0) or 0)
        current_value = current_quantity * price if price > 0 else 0.0
        suggested_weight = _safe_float(rec.get("suggested_weight"))
        target_value = equity * suggested_weight
        recommendation = str(rec.get("recommendation") or "watch")
        proposed_action, suggested_quantity = _propose_action(
            recommendation=recommendation,
            price=price,
            current_quantity=current_quantity,
            sellable_quantity=sellable_quantity,
            current_value=current_value,
            target_value=target_value,
            cash_balance=planning_cash_balance,
            board_lot=int(account["board_lot"]),
        )
        if proposed_action == "BUY":
            planning_cash_balance = max(0.0, planning_cash_balance - suggested_quantity * price * (1 + BUY_FEE_RATE))
        elif proposed_action == "SELL":
            planning_cash_balance += suggested_quantity * price * (1 - SELL_FEE_RATE - SELL_TAX_RATE)
        rows.append(
            {
                "plan_id": plan_id,
                "symbol": symbol,
                "proposed_action": proposed_action,
                "recommendation": recommendation,
                "score": _safe_float(rec.get("score")),
                "confidence": _safe_float(rec.get("confidence")),
                "risk_level": str(rec.get("risk_level") or "unknown"),
                "suggested_weight": suggested_weight,
                "current_quantity": current_quantity,
                "sellable_quantity": sellable_quantity,
                "latest_price": price if price > 0 else None,
                "current_value": current_value,
                "target_value": target_value,
                "suggested_quantity": int(suggested_quantity),
                "rationale_json": json.dumps(
                    {
                        "source": "agent_recommendation",
                        "source_run_id": rec.get("run_id"),
                        "source_as_of_date": str(rec.get("as_of_date")),
                        "source_expected_previous_trading_day": source_expected_date.isoformat(),
                        "raw_rationale_json": rec.get("rationale_json"),
                    },
                    sort_keys=True,
                    default=str,
                ),
            }
        )

    summary = {
        "account_id": DEFAULT_ACCOUNT_ID,
        "plan_date": plan_date.isoformat(),
        "source_as_of_date": str(recommendations["as_of_date"].max()),
        "expected_previous_trading_day": source_expected_date.isoformat(),
        "source_run_id": str(recommendations["run_id"].iloc[0]),
        "cash_balance": cash_balance,
        "equity": equity,
        "action_counts": _action_counts(rows),
        "settlement_rule": "T+2.5",
    }
    with get_connection(read_only=False) as conn:
        conn.execute(
            """
            INSERT INTO demo_trading_daily_plans (
                plan_id, account_id, plan_date, source_run_id,
                source_as_of_date, status, summary_json
            )
            VALUES (?, ?, ?, ?, ?, 'active', ?)
            """,
            [
                plan_id,
                DEFAULT_ACCOUNT_ID,
                plan_date,
                summary["source_run_id"],
                pd.Timestamp(summary["source_as_of_date"]).date(),
                json.dumps(summary, sort_keys=True),
            ],
        )
        for row in rows:
            conn.execute(
                """
                INSERT INTO demo_trading_plan_items (
                    plan_id, symbol, proposed_action, recommendation, score,
                    confidence, risk_level, suggested_weight, current_quantity,
                    sellable_quantity, latest_price, current_value, target_value,
                    suggested_quantity, rationale_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    row["plan_id"],
                    row["symbol"],
                    row["proposed_action"],
                    row["recommendation"],
                    row["score"],
                    row["confidence"],
                    row["risk_level"],
                    row["suggested_weight"],
                    row["current_quantity"],
                    row["sellable_quantity"],
                    row["latest_price"],
                    row["current_value"],
                    row["target_value"],
                    row["suggested_quantity"],
                    row["rationale_json"],
                ],
            )
    LOGGER.log_web_event(
        "Generated demo trading daily plan",
        event_type="demo_trading_plan",
        status="success",
        plan_id=plan_id,
        item_count=len(rows),
        action_counts=summary["action_counts"],
    )
    return {"plan_id": plan_id, "status": "generated", "item_count": len(rows), **summary}


def _current_plan_row(plan_date: date | None = None) -> dict[str, Any] | None:
    """Return today's plan metadata if present."""
    ensure_demo_trading_tables()
    target_date = plan_date or _today()
    with get_connection(read_only=True) as conn:
        row = conn.execute(
            """
            SELECT plan.plan_id,
                   plan.plan_date,
                   plan.source_run_id,
                   plan.source_as_of_date,
                   plan.status,
                   plan.summary_json,
                   count(item.symbol) AS item_count
            FROM demo_trading_daily_plans plan
            LEFT JOIN demo_trading_plan_items item
              ON plan.plan_id = item.plan_id
            WHERE plan.account_id = ?
              AND plan.plan_date = ?
            GROUP BY plan.plan_id, plan.plan_date, plan.source_run_id, plan.source_as_of_date, plan.status, plan.summary_json
            ORDER BY max(plan.generated_at) DESC
            LIMIT 1
            """,
            [DEFAULT_ACCOUNT_ID, target_date],
        ).fetchone()
    if not row:
        return None
    summary = json.loads(row[5] or "{}")
    return {
        "plan_id": row[0],
        "plan_date": str(row[1]),
        "source_run_id": row[2],
        "source_as_of_date": str(row[3]) if row[3] else "",
        "status": row[4],
        "summary": summary,
        "item_count": int(row[6] or 0),
    }


def _portfolio_equity(account: dict[str, Any], positions: list[dict[str, Any]], prices: dict[str, dict[str, Any]]) -> float:
    """Compute account equity from cash plus latest mark-to-market value."""
    market_value = 0.0
    for position in positions:
        price = _safe_float(prices.get(position["symbol"], {}).get("close"))
        market_value += int(position["quantity"]) * price
    return _safe_float(account["cash_balance"]) + market_value


def _propose_action(
    *,
    recommendation: str,
    price: float,
    current_quantity: int,
    sellable_quantity: int,
    current_value: float,
    target_value: float,
    cash_balance: float,
    board_lot: int,
) -> tuple[str, int]:
    """Convert one recommendation and current holding into a demo order action."""
    if price <= 0:
        return "NO_DATA", 0
    if recommendation in {"avoid_or_reduce", "blocked_data_quality"}:
        if sellable_quantity > 0:
            return "SELL", _floor_board_lot(sellable_quantity, board_lot)
        if current_quantity > 0:
            return "HOLD_LOCKED", 0
        return "WATCH", 0
    if recommendation == "candidate_long" and target_value > current_value:
        target_quantity = _floor_board_lot((target_value - current_value) / price, board_lot)
        affordable_quantity = _floor_board_lot(cash_balance / (price * (1 + BUY_FEE_RATE)), board_lot)
        quantity = min(target_quantity, affordable_quantity)
        if quantity > 0:
            return "BUY", quantity
    if current_quantity > 0:
        return "HOLD", 0
    return "WATCH", 0


def _action_counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    """Count proposed actions in a plan item list."""
    counts: dict[str, int] = {}
    for row in rows:
        action = str(row["proposed_action"])
        counts[action] = counts.get(action, 0) + 1
    return counts


def load_demo_trading_snapshot() -> dict[str, Any]:
    """Load account, positions, daily plan, and recent order state for the UI."""
    ensure_demo_trading_tables()
    account = _load_account()
    plan = _current_plan_row()
    positions = _load_positions()
    plan_as_of = pd.Timestamp(plan["source_as_of_date"]).date() if plan and plan.get("source_as_of_date") else None
    prices = _latest_price_map(plan_as_of)
    enriched_positions = []
    total_market_value = 0.0
    total_cost = 0.0
    for position in positions:
        price = _safe_float(prices.get(position["symbol"], {}).get("close"))
        market_value = int(position["quantity"]) * price
        cost_basis = _safe_float(position["cost_basis"])
        total_market_value += market_value
        total_cost += cost_basis
        enriched_positions.append(
            {
                **position,
                "latest_price": price,
                "market_value": market_value,
                "unrealized_pnl": market_value - cost_basis,
                "unrealized_pct": ((market_value - cost_basis) / cost_basis) if cost_basis > 0 else 0.0,
            }
        )
    equity = _safe_float(account["cash_balance"]) + total_market_value
    plan_items = _load_plan_items(plan["plan_id"]) if plan else []
    orders = _load_recent_orders()
    return {
        "account": account,
        "portfolio": {
            "cash_balance": _safe_float(account["cash_balance"]),
            "market_value": total_market_value,
            "equity": equity,
            "total_cost": total_cost,
            "unrealized_pnl": total_market_value - total_cost,
            "unrealized_pct": ((total_market_value - total_cost) / total_cost) if total_cost > 0 else 0.0,
        },
        "positions": enriched_positions,
        "plan": plan,
        "plan_items": plan_items,
        "orders": orders,
    }


def _load_plan_items(plan_id: str) -> list[dict[str, Any]]:
    """Load plan items for display and execution."""
    with get_connection(read_only=True) as conn:
        rows = conn.execute(
            """
            SELECT symbol, proposed_action, recommendation, score, confidence,
                   risk_level, suggested_weight, current_quantity, sellable_quantity,
                   latest_price, current_value, target_value, suggested_quantity,
                   rationale_json
            FROM demo_trading_plan_items
            WHERE plan_id = ?
            ORDER BY
                CASE proposed_action
                    WHEN 'SELL' THEN 1
                    WHEN 'BUY' THEN 2
                    WHEN 'HOLD_LOCKED' THEN 3
                    WHEN 'HOLD' THEN 4
                    ELSE 5
                END,
                suggested_weight DESC,
                score DESC,
                symbol
            """,
            [plan_id],
        ).fetchall()
    return [
        {
            "symbol": row[0],
            "proposed_action": row[1],
            "recommendation": row[2],
            "score": _safe_float(row[3]),
            "confidence": _safe_float(row[4]),
            "risk_level": row[5],
            "suggested_weight": _safe_float(row[6]),
            "current_quantity": int(row[7] or 0),
            "sellable_quantity": int(row[8] or 0),
            "latest_price": _safe_float(row[9]),
            "current_value": _safe_float(row[10]),
            "target_value": _safe_float(row[11]),
            "suggested_quantity": int(row[12] or 0),
            "rationale_json": row[13] or "{}",
        }
        for row in rows
    ]


def _load_recent_orders(limit: int = 30) -> list[dict[str, Any]]:
    """Load recent demo orders."""
    with get_connection(read_only=True) as conn:
        rows = conn.execute(
            """
            SELECT trade_time, action, symbol, quantity, price, gross_amount,
                   fees, taxes, net_amount, realized_pnl, status, settlement_ts, source
            FROM demo_trading_orders
            WHERE account_id = ?
            ORDER BY trade_time DESC, created_at DESC
            LIMIT ?
            """,
            [DEFAULT_ACCOUNT_ID, int(limit)],
        ).fetchall()
    return [
        {
            "trade_time": str(row[0]),
            "action": row[1],
            "symbol": row[2],
            "quantity": int(row[3] or 0),
            "price": _safe_float(row[4]),
            "gross_amount": _safe_float(row[5]),
            "fees": _safe_float(row[6]),
            "taxes": _safe_float(row[7]),
            "net_amount": _safe_float(row[8]),
            "realized_pnl": _safe_float(row[9]),
            "status": row[10],
            "settlement_ts": str(row[11]) if row[11] else "",
            "source": row[12],
        }
        for row in rows
    ]


def execute_plan_item(symbol: str) -> dict[str, Any]:
    """Let the agent execute one actionable daily-plan item."""
    snapshot = load_demo_trading_snapshot()
    plan = snapshot.get("plan")
    if not plan:
        raise RuntimeError("No active daily plan. Generate a plan first.")
    item = next((row for row in snapshot["plan_items"] if row["symbol"] == symbol), None)
    if not item:
        raise ValueError(f"Symbol {symbol} is not in the active plan")
    if item["proposed_action"] not in {"BUY", "SELL"}:
        raise ValueError(f"{symbol} action is {item['proposed_action']}; no order is required")
    return place_demo_order(
        action=item["proposed_action"],
        symbol=symbol,
        quantity=int(item["suggested_quantity"]),
        price_override=_safe_float(item["latest_price"]),
        source="agent_daily_plan",
        source_run_id=plan.get("source_run_id"),
        recommendation=item["recommendation"],
        rationale_json=item["rationale_json"],
    )


def execute_all_plan_items() -> dict[str, Any]:
    """Execute every actionable BUY/SELL item in the active daily plan."""
    snapshot = load_demo_trading_snapshot()
    results = []
    for item in snapshot["plan_items"]:
        if item["proposed_action"] in {"BUY", "SELL"} and int(item["suggested_quantity"]) > 0:
            try:
                results.append({"symbol": item["symbol"], **execute_plan_item(item["symbol"])})
            except Exception as exc:
                results.append({"symbol": item["symbol"], "status": "failed", "error": str(exc)})
    return {
        "attempted": len(results),
        "filled": sum(1 for row in results if row.get("status") == "filled"),
        "results": results,
    }


def place_demo_order(
    *,
    action: str,
    symbol: str,
    quantity: int,
    price_override: float | None = None,
    source: str,
    source_run_id: str | None = None,
    recommendation: str | None = None,
    rationale_json: str | None = None,
) -> dict[str, Any]:
    """Place a real order in the demo account ledger.

    Args:
        action: `BUY` or `SELL`.
        symbol: VN30 ticker.
        quantity: Board-lot quantity to fill.
        price_override: Optional VND/share price. Agent plan execution passes
            the plan's previous-trading-day price so fills stay tied to the
            daily recommendation snapshot.
        source: Order source, e.g. `agent_daily_plan`.
        source_run_id: Optional agent run id.
        recommendation: Recommendation state that produced the order.
        rationale_json: Optional plan/recommendation rationale.

    Returns:
        Filled order summary.

    Raises:
        ValueError: If order quantity/cash/settlement constraints fail.
    """
    ensure_demo_trading_tables()
    action = action.upper()
    symbol = symbol.upper().strip()
    quantity = _floor_board_lot(quantity, BOARD_LOT)
    if action not in {"BUY", "SELL"}:
        raise ValueError(f"Unsupported demo action: {action}")
    if not symbol or quantity <= 0:
        raise ValueError("Order quantity must be a positive board-lot multiple")
    price = _safe_float(price_override)
    if price <= 0:
        price_meta = _latest_price_map().get(symbol)
        price = _safe_float(price_meta.get("close") if price_meta else None)
    if price <= 0:
        raise ValueError(f"No latest price available for {symbol}")
    if action == "BUY":
        return _place_buy_order(
            symbol=symbol,
            quantity=quantity,
            price=price,
            source=source,
            source_run_id=source_run_id,
            recommendation=recommendation,
            rationale_json=rationale_json,
        )
    return _place_sell_order(
        symbol=symbol,
        quantity=quantity,
        price=price,
        source=source,
        source_run_id=source_run_id,
        recommendation=recommendation,
        rationale_json=rationale_json,
    )


def _place_buy_order(
    *,
    symbol: str,
    quantity: int,
    price: float,
    source: str,
    source_run_id: str | None,
    recommendation: str | None,
    rationale_json: str | None,
) -> dict[str, Any]:
    """Fill a BUY order and create a locked T+2.5 lot."""
    account = _load_account()
    gross = price * quantity
    fees = gross * BUY_FEE_RATE
    net = gross + fees
    if _safe_float(account["cash_balance"]) < net:
        raise ValueError(f"Insufficient demo cash for BUY {symbol}: need {net:,.0f} VND")
    order_id = str(uuid.uuid4())
    lot_id = str(uuid.uuid4())
    trade_time = now_local().replace(tzinfo=None)
    trade_date = trade_time.date()
    settlement_ts = _settlement_timestamp(trade_date)
    with get_connection(read_only=False) as conn:
        conn.execute(
            """
            UPDATE demo_trading_accounts
            SET cash_balance = cash_balance - ?, updated_at = CURRENT_TIMESTAMP
            WHERE account_id = ?
            """,
            [net, DEFAULT_ACCOUNT_ID],
        )
        conn.execute(
            """
            INSERT INTO demo_trading_orders (
                order_id, account_id, action, symbol, trade_date, trade_time,
                price, quantity, gross_amount, fees, taxes, net_amount,
                realized_pnl, status, settlement_date, settlement_ts, source,
                source_run_id, recommendation, rationale_json
            )
            VALUES (?, ?, 'BUY', ?, ?, ?, ?, ?, ?, ?, 0, ?, 0, 'filled', ?, ?, ?, ?, ?, ?)
            """,
            [
                order_id,
                DEFAULT_ACCOUNT_ID,
                symbol,
                trade_date,
                trade_time,
                price,
                quantity,
                gross,
                fees,
                net,
                settlement_ts.date(),
                settlement_ts,
                source,
                source_run_id,
                recommendation,
                rationale_json or "{}",
            ],
        )
        conn.execute(
            """
            INSERT INTO demo_trading_lots (
                lot_id, account_id, symbol, buy_order_id, trade_date,
                settlement_date, settlement_ts, quantity, remaining_quantity,
                avg_cost, status
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open')
            """,
            [
                lot_id,
                DEFAULT_ACCOUNT_ID,
                symbol,
                order_id,
                trade_date,
                settlement_ts.date(),
                settlement_ts,
                quantity,
                quantity,
                net / quantity,
            ],
        )
    return {
        "order_id": order_id,
        "status": "filled",
        "action": "BUY",
        "symbol": symbol,
        "quantity": quantity,
        "price": price,
        "net_amount": net,
        "settlement_ts": settlement_ts.isoformat(),
    }


def _place_sell_order(
    *,
    symbol: str,
    quantity: int,
    price: float,
    source: str,
    source_run_id: str | None,
    recommendation: str | None,
    rationale_json: str | None,
) -> dict[str, Any]:
    """Fill a SELL order using FIFO sellable lots."""
    sellable_lots = _sellable_lots(symbol)
    available = sum(int(row["remaining_quantity"]) for row in sellable_lots)
    if quantity > available:
        raise ValueError(f"Only {available:,} settled shares of {symbol} are sellable under T+2.5")
    gross = price * quantity
    fees = gross * SELL_FEE_RATE
    taxes = gross * SELL_TAX_RATE
    net = gross - fees - taxes
    realized_pnl = 0.0
    remaining_to_sell = quantity
    updates: list[tuple[int, str, str]] = []
    for lot in sellable_lots:
        if remaining_to_sell <= 0:
            break
        lot_qty = int(lot["remaining_quantity"])
        sell_qty = min(lot_qty, remaining_to_sell)
        realized_pnl += sell_qty * (price - _safe_float(lot["avg_cost"]))
        new_remaining = lot_qty - sell_qty
        status = "closed" if new_remaining == 0 else "open"
        updates.append((new_remaining, status, lot["lot_id"]))
        remaining_to_sell -= sell_qty
    realized_pnl -= fees + taxes

    order_id = str(uuid.uuid4())
    trade_time = now_local().replace(tzinfo=None)
    trade_date = trade_time.date()
    settlement_ts = _settlement_timestamp(trade_date)
    with get_connection(read_only=False) as conn:
        for new_remaining, status, lot_id in updates:
            conn.execute(
                """
                UPDATE demo_trading_lots
                SET remaining_quantity = ?, status = ?, updated_at = CURRENT_TIMESTAMP
                WHERE lot_id = ?
                """,
                [new_remaining, status, lot_id],
            )
        conn.execute(
            """
            UPDATE demo_trading_accounts
            SET cash_balance = cash_balance + ?, updated_at = CURRENT_TIMESTAMP
            WHERE account_id = ?
            """,
            [net, DEFAULT_ACCOUNT_ID],
        )
        conn.execute(
            """
            INSERT INTO demo_trading_orders (
                order_id, account_id, action, symbol, trade_date, trade_time,
                price, quantity, gross_amount, fees, taxes, net_amount,
                realized_pnl, status, settlement_date, settlement_ts, source,
                source_run_id, recommendation, rationale_json
            )
            VALUES (?, ?, 'SELL', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'filled', ?, ?, ?, ?, ?, ?)
            """,
            [
                order_id,
                DEFAULT_ACCOUNT_ID,
                symbol,
                trade_date,
                trade_time,
                price,
                quantity,
                gross,
                fees,
                taxes,
                net,
                realized_pnl,
                settlement_ts.date(),
                settlement_ts,
                source,
                source_run_id,
                recommendation,
                rationale_json or "{}",
            ],
        )
    return {
        "order_id": order_id,
        "status": "filled",
        "action": "SELL",
        "symbol": symbol,
        "quantity": quantity,
        "price": price,
        "net_amount": net,
        "realized_pnl": realized_pnl,
        "settlement_ts": settlement_ts.isoformat(),
    }


def _sellable_lots(symbol: str) -> list[dict[str, Any]]:
    """Load FIFO settled lots for a symbol."""
    current = now_local().replace(tzinfo=None)
    with get_connection(read_only=True) as conn:
        rows = conn.execute(
            """
            SELECT lot_id, remaining_quantity, avg_cost
            FROM demo_trading_lots
            WHERE account_id = ?
              AND symbol = ?
              AND remaining_quantity > 0
              AND status = 'open'
              AND settlement_ts <= ?
            ORDER BY trade_date, created_at, lot_id
            """,
            [DEFAULT_ACCOUNT_ID, symbol, current],
        ).fetchall()
    return [
        {"lot_id": row[0], "remaining_quantity": int(row[1] or 0), "avg_cost": _safe_float(row[2])}
        for row in rows
    ]


def reset_demo_account() -> dict[str, Any]:
    """Reset the demo account to 50,000,000 VND cash and no positions."""
    ensure_demo_trading_tables()
    with get_connection(read_only=False) as conn:
        conn.execute(
            """
            DELETE FROM demo_trading_plan_items
            WHERE plan_id IN (
                SELECT plan_id FROM demo_trading_daily_plans WHERE account_id = ?
            )
            """,
            [DEFAULT_ACCOUNT_ID],
        )
        for table in ["demo_trading_daily_plans", "demo_trading_lots", "demo_trading_orders"]:
            conn.execute(f"DELETE FROM {table} WHERE account_id = ?", [DEFAULT_ACCOUNT_ID])
        conn.execute(
            """
            UPDATE demo_trading_accounts
            SET cash_balance = initial_cash, updated_at = CURRENT_TIMESTAMP
            WHERE account_id = ?
            """,
            [DEFAULT_ACCOUNT_ID],
        )
    return {"status": "reset", "cash_balance": INITIAL_CASH}
