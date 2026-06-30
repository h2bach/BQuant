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
DEFAULT_ANALYSIS_ENTRY_DATE = date(2026, 6, 15)
DEFAULT_ANALYSIS_AS_OF_DATE = date(2026, 6, 30)
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
        conn.execute(
            """
            CREATE OR REPLACE VIEW v_demo_trading_trade_history AS
            SELECT
                account_id,
                trade_time,
                trade_date,
                action,
                symbol,
                quantity,
                price,
                gross_amount,
                fees,
                taxes,
                net_amount,
                realized_pnl,
                status,
                settlement_date,
                settlement_ts,
                source,
                source_run_id,
                recommendation,
                rationale_json,
                created_at
            FROM demo_trading_orders
            """
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


def _trading_days_elapsed(start_date: date, end_date: date) -> int:
    """Count trading days elapsed after a trade date.

    Args:
        start_date: Trade date of a lot.
        end_date: Current local market date.

    Returns:
        Number of configured trading days between `start_date` exclusive and
        `end_date` inclusive. A same-day lot is `0`, the next trading day is
        `1`, and the second trading day before the 13:00 settlement cutoff is
        `2`.
    """
    if end_date <= start_date:
        return 0
    current = start_date
    elapsed = 0
    while current < end_date:
        current += timedelta(days=1)
        if is_trading_day(current):
            elapsed += 1
    return elapsed


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


def _safe_json(value: Any) -> dict[str, Any]:
    """Parse a JSON object field into a dictionary.

    Args:
        value: Raw JSON string, dictionary, or null-like value from DuckDB.

    Returns:
        Parsed dictionary. Invalid JSON returns an empty dictionary so analysis
        rendering remains resilient to malformed rationale payloads.
    """
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(str(value or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _coerce_date(value: Any, default: date) -> date:
    """Convert user/date-control input into a Python date.

    Args:
        value: Date, datetime, pandas timestamp, ISO date string, or null.
        default: Fallback date when conversion fails.

    Returns:
        Valid date for warehouse queries.
    """
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return pd.Timestamp(value).date()
    except (TypeError, ValueError):
        return default


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
    """Load current open lots aggregated by symbol with T+ settlement buckets."""
    current = (moment or now_local()).replace(tzinfo=None)
    ensure_demo_trading_tables()
    with get_connection(read_only=True) as conn:
        rows = conn.execute(
            """
            SELECT symbol,
                   trade_date,
                   settlement_ts,
                   remaining_quantity,
                   avg_cost
            FROM demo_trading_lots
            WHERE account_id = ?
              AND remaining_quantity > 0
              AND status = 'open'
            ORDER BY symbol, trade_date, created_at, lot_id
            """,
            [DEFAULT_ACCOUNT_ID],
        ).fetchall()
    positions: dict[str, dict[str, Any]] = {}
    for symbol_raw, trade_date_raw, settlement_ts_raw, quantity_raw, avg_cost_raw in rows:
        symbol = str(symbol_raw)
        quantity = int(quantity_raw or 0)
        avg_cost = _safe_float(avg_cost_raw)
        trade_date = pd.Timestamp(trade_date_raw).date()
        settlement_ts = pd.Timestamp(settlement_ts_raw).to_pydatetime()
        bucket = "sellable_quantity"
        if settlement_ts > current:
            age = _trading_days_elapsed(trade_date, current.date())
            if age <= 0:
                bucket = "t0_quantity"
            elif age == 1:
                bucket = "t1_quantity"
            else:
                bucket = "t2_quantity"

        position = positions.setdefault(
            symbol,
            {
                "symbol": symbol,
                "quantity": 0,
                "t0_quantity": 0,
                "t1_quantity": 0,
                "t2_quantity": 0,
                "sellable_quantity": 0,
                "avg_cost": 0.0,
                "cost_basis": 0.0,
                "first_settlement_ts": "",
                "last_settlement_ts": "",
            },
        )
        position["quantity"] += quantity
        position[bucket] += quantity
        position["cost_basis"] += quantity * avg_cost
        position["first_settlement_ts"] = (
            str(settlement_ts)
            if not position["first_settlement_ts"] or str(settlement_ts) < position["first_settlement_ts"]
            else position["first_settlement_ts"]
        )
        position["last_settlement_ts"] = (
            str(settlement_ts)
            if not position["last_settlement_ts"] or str(settlement_ts) > position["last_settlement_ts"]
            else position["last_settlement_ts"]
        )

    for position in positions.values():
        quantity = int(position["quantity"])
        position["avg_cost"] = position["cost_basis"] / quantity if quantity > 0 else 0.0
    return sorted(positions.values(), key=lambda item: item["symbol"])


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


def add_symbol_to_portfolio(symbol: str, quantity: int) -> dict[str, Any]:
    """Buy a requested quantity of one symbol in the demo ledger.

    Args:
        symbol: VN30 ticker to add or top up. The value is normalized to uppercase
            and must have a latest close in `daily_ohlcv_base`.
        quantity: Number of shares to buy. The value must be a positive multiple
            of the account board lot, currently 100 shares.

    Returns:
        Filled BUY order summary plus latest-price metadata.

    Raises:
        ValueError: If the symbol has no latest price, quantity is invalid, or
            available cash is insufficient.

    Side Effects:
        Places a normal demo BUY order and creates a T+2.5-settled lot. The
        order is recorded in `demo_trading_orders` and appears in trade
        history.
    """
    ensure_demo_trading_tables()
    resolved_symbol = symbol.upper().strip()
    if not resolved_symbol:
        raise ValueError("Symbol is required")

    requested_quantity = int(_safe_float(quantity))
    if requested_quantity <= 0:
        raise ValueError("Quantity must be positive")

    account = _load_account()
    board_lot = int(account["board_lot"])
    if requested_quantity % board_lot != 0:
        raise ValueError(f"Quantity must be a multiple of the board lot ({board_lot})")

    prices = _latest_price_map()
    price_meta = prices.get(resolved_symbol)
    price = _safe_float(price_meta.get("close") if price_meta else None)
    if price <= 0:
        raise ValueError(f"No latest price available for {resolved_symbol}")

    rationale = {
        "source": "manual_add_symbol",
        "requested_quantity": requested_quantity,
        "cash_balance": _safe_float(account["cash_balance"]),
        "board_lot": board_lot,
        "latest_price_date": str(price_meta.get("trading_date") if price_meta else ""),
    }
    order = place_demo_order(
        action="BUY",
        symbol=resolved_symbol,
        quantity=requested_quantity,
        price_override=price,
        source="manual_add_symbol",
        recommendation="manual_quantity_order",
        rationale_json=json.dumps(rationale, sort_keys=True),
    )
    order.update(
        {
            "latest_price_date": str(price_meta.get("trading_date") if price_meta else ""),
        }
    )
    LOGGER.log_web_event(
        "Added symbol to demo trading portfolio",
        event_type="demo_trading_add_symbol",
        status="success",
        symbol=resolved_symbol,
        quantity=requested_quantity,
        price=round(price, 2),
    )
    return order


def place_manual_trade(*, action: str, symbol: str, quantity: int, price: float) -> dict[str, Any]:
    """Place an explicit manual BUY/SELL order from the PnL table.

    Args:
        action: Trade side, either `BUY` or `SELL`.
        symbol: Ticker to trade.
        quantity: Share quantity. Must be a positive board-lot multiple.
        price: Explicit VND/share execution price supplied by the UI ticket.

    Returns:
        Filled order summary from the demo ledger.

    Raises:
        ValueError: If side, symbol, quantity, price, cash, or sellable-lot
            constraints are invalid.

    Side Effects:
        Writes one order to `demo_trading_orders`, updates cash, and opens or
        reduces lots. A full SELL closes the lots and the symbol disappears from
        current positions naturally when remaining quantity reaches zero.
    """
    resolved_action = action.upper().strip()
    resolved_symbol = symbol.upper().strip()
    resolved_quantity = int(_safe_float(quantity))
    resolved_price = _safe_float(price)
    if resolved_action not in {"BUY", "SELL"}:
        raise ValueError("Manual trade action must be BUY or SELL")
    if not resolved_symbol:
        raise ValueError("Symbol is required")
    if resolved_quantity <= 0:
        raise ValueError("Quantity must be positive")
    if resolved_quantity % BOARD_LOT != 0:
        raise ValueError(f"Quantity must be a multiple of the board lot ({BOARD_LOT})")
    if resolved_price <= 0:
        raise ValueError("Execution price must be positive")

    rationale = {
        "source": "manual_pnl_table",
        "explicit_price": resolved_price,
        "explicit_quantity": resolved_quantity,
        "board_lot": BOARD_LOT,
    }
    return place_demo_order(
        action=resolved_action,
        symbol=resolved_symbol,
        quantity=resolved_quantity,
        price_override=resolved_price,
        source="manual_pnl_table",
        recommendation="manual_trade_ticket",
        rationale_json=json.dumps(rationale, sort_keys=True),
    )


def estimate_manual_trade(*, action: str, symbol: str, quantity: int, price: float | None = None) -> dict[str, Any]:
    """Estimate a manual trade before execution.

    Args:
        action: Trade side, `BUY` or `SELL`.
        symbol: Ticker to estimate.
        quantity: Requested share quantity.
        price: Optional explicit VND/share execution price. When absent or
            non-positive, the latest daily close is used when available.

    Returns:
        Preview payload containing price, gross amount, fees, taxes, net amount,
        estimated cash balance after the trade, sellable quantity, and estimated
        average cost after a BUY.

    Side Effects:
        Reads account, position, and latest price state only; does not write
        the ledger.
    """
    ensure_demo_trading_tables()
    resolved_action = action.upper().strip()
    resolved_symbol = symbol.upper().strip()
    resolved_quantity = int(_safe_float(quantity))
    account = _load_account()
    positions = {row["symbol"]: row for row in _load_positions()}
    current_position = positions.get(resolved_symbol, {})
    latest_price_meta = _latest_price_map().get(resolved_symbol, {})
    resolved_price = _safe_float(price)
    if resolved_price <= 0:
        resolved_price = _safe_float(latest_price_meta.get("close"))

    gross = max(0, resolved_quantity) * max(0.0, resolved_price)
    fees = 0.0
    taxes = 0.0
    net_amount = 0.0
    cash_change = 0.0
    validation_message = ""
    if resolved_action == "BUY":
        fees = gross * BUY_FEE_RATE
        net_amount = gross + fees
        cash_change = -net_amount
    elif resolved_action == "SELL":
        fees = gross * SELL_FEE_RATE
        taxes = gross * SELL_TAX_RATE
        net_amount = gross - fees - taxes
        cash_change = net_amount
    else:
        validation_message = "Action must be BUY or SELL"

    cash_balance = _safe_float(account["cash_balance"])
    current_quantity = int(current_position.get("quantity", 0) or 0)
    sellable_quantity = int(current_position.get("sellable_quantity", 0) or 0)
    current_cost_basis = _safe_float(current_position.get("cost_basis"))
    current_avg_cost = _safe_float(current_position.get("avg_cost"))
    estimated_avg_cost = current_avg_cost
    estimated_realized_pnl = 0.0
    if resolved_action == "BUY" and resolved_quantity > 0:
        estimated_quantity = current_quantity + resolved_quantity
        estimated_avg_cost = (current_cost_basis + net_amount) / estimated_quantity if estimated_quantity > 0 else 0.0
    elif resolved_action == "SELL" and resolved_quantity > 0:
        remaining_to_sell = min(resolved_quantity, sellable_quantity)
        sold_cost_basis = 0.0
        for lot in _sellable_lots(resolved_symbol):
            if remaining_to_sell <= 0:
                break
            lot_quantity = int(lot["remaining_quantity"])
            sell_quantity = min(lot_quantity, remaining_to_sell)
            sold_cost_basis += sell_quantity * _safe_float(lot["avg_cost"])
            remaining_to_sell -= sell_quantity
        estimated_realized_pnl = net_amount - sold_cost_basis if resolved_quantity <= sellable_quantity else 0.0
        estimated_remaining_quantity = max(0, current_quantity - min(resolved_quantity, sellable_quantity))
        estimated_remaining_cost = max(0.0, current_cost_basis - sold_cost_basis)
        estimated_avg_cost = (
            estimated_remaining_cost / estimated_remaining_quantity
            if estimated_remaining_quantity > 0
            else 0.0
        )

    if not validation_message:
        if not resolved_symbol:
            validation_message = "Symbol is required"
        elif resolved_quantity <= 0:
            validation_message = "Quantity must be positive"
        elif resolved_quantity % BOARD_LOT != 0:
            validation_message = f"Quantity must be a multiple of the board lot ({BOARD_LOT})"
        elif resolved_price <= 0:
            validation_message = "Execution price must be positive"
        elif resolved_action == "BUY" and cash_balance < net_amount:
            validation_message = f"Insufficient cash; need {net_amount:,.0f} VND"
        elif resolved_action == "SELL" and resolved_quantity > sellable_quantity:
            validation_message = f"Only {sellable_quantity:,} shares are sellable under T+2.5"

    return {
        "action": resolved_action,
        "symbol": resolved_symbol,
        "quantity": resolved_quantity,
        "price": resolved_price,
        "latest_price_date": str(latest_price_meta.get("trading_date") or ""),
        "gross_amount": gross,
        "fees": fees,
        "taxes": taxes,
        "net_amount": net_amount,
        "cash_balance": cash_balance,
        "cash_change": cash_change,
        "cash_after": cash_balance + cash_change,
        "current_quantity": current_quantity,
        "sellable_quantity": sellable_quantity,
        "current_avg_cost": current_avg_cost,
        "estimated_avg_cost": estimated_avg_cost,
        "estimated_realized_pnl": estimated_realized_pnl,
        "is_valid": not validation_message,
        "validation_message": validation_message,
    }


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


def _historical_price_points(symbols: list[str], entry_date: date, as_of_date: date) -> dict[str, dict[str, Any]]:
    """Load entry and current prices for a list of symbols.

    Args:
        symbols: Tickers to analyze.
        entry_date: Desired buy date. The query uses the first trading row on
            or after this date, capped by `as_of_date`.
        as_of_date: Current data ceiling. The query uses the latest trading row
            on or before this date.

    Returns:
        Mapping keyed by symbol with entry/current date and VND/share prices.
    """
    if not symbols:
        return {}
    placeholders = ", ".join(["?"] * len(symbols))
    with get_connection(read_only=True) as conn:
        rows = conn.execute(
            f"""
            WITH entry_ranked AS (
                SELECT
                    symbol,
                    trading_date,
                    open AS entry_open,
                    row_number() OVER (PARTITION BY symbol ORDER BY trading_date ASC) AS rn
                FROM daily_ohlcv_base
                WHERE symbol IN ({placeholders})
                  AND trading_date >= ?
                  AND trading_date <= ?
            ),
            current_ranked AS (
                SELECT
                    symbol,
                    trading_date,
                    close AS current_close,
                    row_number() OVER (PARTITION BY symbol ORDER BY trading_date DESC) AS rn
                FROM daily_ohlcv_base
                WHERE symbol IN ({placeholders})
                  AND trading_date <= ?
            )
            SELECT
                coalesce(entry_ranked.symbol, current_ranked.symbol) AS symbol,
                entry_ranked.trading_date AS entry_date,
                entry_ranked.entry_open,
                current_ranked.trading_date AS current_date,
                current_ranked.current_close
            FROM entry_ranked
            FULL OUTER JOIN current_ranked
              ON entry_ranked.symbol = current_ranked.symbol
             AND entry_ranked.rn = 1
             AND current_ranked.rn = 1
            WHERE coalesce(entry_ranked.rn, 1) = 1
              AND coalesce(current_ranked.rn, 1) = 1
            """,
            [*symbols, entry_date, as_of_date, *symbols, as_of_date],
        ).fetchall()
    return {
        str(row[0]): {
            "entry_date": pd.Timestamp(row[1]).date() if row[1] else None,
            "entry_price": _to_vnd_price(row[2]),
            "current_date": pd.Timestamp(row[3]).date() if row[3] else None,
            "current_price": _to_vnd_price(row[4]),
        }
        for row in rows
    }


def _context_rows(symbols: list[str], as_of_date: date) -> dict[str, dict[str, Any]]:
    """Load latest dbt agent context rows for symbols.

    Args:
        symbols: Symbols to fetch from `analytics_marts.mart_agent_context_daily`.
        as_of_date: Latest allowed context date.

    Returns:
        Mapping keyed by symbol with trend, liquidity, return, volatility, and
        market-regime features.
    """
    if not symbols:
        return {}
    placeholders = ", ".join(["?"] * len(symbols))
    with get_connection(read_only=True) as conn:
        rows = conn.execute(
            f"""
            WITH ranked AS (
                SELECT
                    symbol,
                    trading_date,
                    close,
                    return_1d,
                    return_5d,
                    return_20d,
                    return_60d,
                    volatility_20d,
                    volatility_60d,
                    drawdown_from_peak,
                    relative_strength_20d,
                    relative_strength_60d,
                    traded_value_cross_section_percentile,
                    volume_to_avg_20d,
                    trend_state,
                    liquidity_state,
                    market_regime,
                    volatility_regime,
                    regime_score,
                    data_quality_status,
                    stale_days,
                    row_number() OVER (PARTITION BY symbol ORDER BY trading_date DESC) AS rn
                FROM analytics_marts.mart_agent_context_daily
                WHERE symbol IN ({placeholders})
                  AND trading_date <= ?
            )
            SELECT *
            FROM ranked
            WHERE rn = 1
            """,
            [*symbols, as_of_date],
        ).df()
    return {str(row["symbol"]): row.to_dict() for _, row in rows.iterrows()}


def _recommendation_rows(symbols: list[str], as_of_date: date) -> dict[str, dict[str, Any]]:
    """Load latest agent recommendations for the requested symbols.

    Args:
        symbols: Symbols to keep from the latest recommendation run.
        as_of_date: Latest allowed recommendation date.

    Returns:
        Mapping keyed by symbol with score, confidence, risk, recommendation,
        and parsed rationale JSON.
    """
    if not symbols:
        return {}
    frame = _latest_recommendations(max_as_of_date=as_of_date)
    if frame.empty:
        return {}
    filtered = frame[frame["symbol"].astype(str).isin(symbols)].copy()
    result: dict[str, dict[str, Any]] = {}
    for _, row in filtered.iterrows():
        item = row.to_dict()
        item["rationale"] = _safe_json(item.get("rationale_json"))
        result[str(item["symbol"])] = item
    return result


def _trend_bucket(value: float, *, neutral_band: float = 0.005) -> str:
    """Classify a return-like value for UI color rendering."""
    if value > neutral_band:
        return "positive"
    if value < -neutral_band:
        return "negative"
    return "neutral"


def _suggest_portfolio_action(
    *,
    recommendation: str,
    risk_level: str,
    trend_state: str,
    gross_return: float,
    return_5d: float,
    score: float,
) -> str:
    """Convert current agent state and PnL into a portfolio action bucket."""
    if recommendation in {"avoid_or_reduce", "blocked_data_quality"} or score <= 0.30:
        return "SELL"
    if trend_state == "downtrend" and (gross_return < -0.03 or return_5d < -0.02):
        return "SELL"
    if risk_level == "high" and (trend_state != "uptrend" or return_5d < 0):
        return "TRIM"
    if recommendation == "candidate_long" or score >= 0.45:
        return "HOLD"
    return "WATCH"


def _analysis_reasons(
    *,
    symbol: str,
    recommendation: str,
    score: float,
    confidence: float,
    risk_level: str,
    trend_state: str,
    liquidity_state: str,
    gross_return: float,
    return_5d: float,
    return_20d: float,
    relative_strength_20d: float,
    component_scores: dict[str, Any],
    action: str,
) -> dict[str, str]:
    """Build concise Vietnamese explanations for buy/hold/sell decisions."""
    trend_score = _safe_float(component_scores.get("trend"), default=0.5)
    momentum_score = _safe_float(component_scores.get("momentum"), default=0.5)
    liquidity_score = _safe_float(component_scores.get("liquidity"), default=0.5)
    buy_reason = (
        f"Mở mua {symbol} theo watchlist/risk-adjusted deployment: score {score:.3f}, "
        f"confidence {confidence:.3f}, trend={trend_state}, risk={risk_level}. "
        f"Component trend/momentum/liquidity lần lượt {trend_score:.2f}/{momentum_score:.2f}/{liquidity_score:.2f}."
    )
    hold_reason = (
        f"Nắm giữ khi recommendation hiện tại là {recommendation}, PnL từ điểm mua {gross_return:+.2%}, "
        f"return 5D {return_5d:+.2%}, return 20D {return_20d:+.2%}, RS20 {relative_strength_20d:+.2%}, "
        f"liquidity={liquidity_state}."
    )
    if action == "SELL":
        sell_reason = (
            "Ưu tiên bán nếu tín hiệu hiện tại chuyển sang avoid/reduce, score yếu, hoặc downtrend kèm mất động lượng. "
            f"Trạng thái hiện tại: action={action}, trend={trend_state}, risk={risk_level}."
        )
    elif action == "TRIM":
        sell_reason = (
            "Chưa cần bán toàn bộ, nhưng nên giảm tỷ trọng nếu rủi ro cao tiếp tục đi kèm động lượng ngắn hạn yếu. "
            f"Trạng thái hiện tại: action={action}, risk={risk_level}, return 5D {return_5d:+.2%}."
        )
    else:
        sell_reason = (
            "Chưa có sell trigger rõ ràng; sell trigger cần theo dõi là score giảm dưới ngưỡng, trend chuyển downtrend, "
            "hoặc thanh khoản/yếu tố relative strength xấu đi."
        )
    return {"buy": buy_reason, "hold": hold_reason, "sell": sell_reason}


def load_demo_portfolio_analysis(
    *,
    entry_date: date | str | None = None,
    as_of_date: date | str | None = None,
    positions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Calculate portfolio PnL and current agent analysis for demo holdings.

    Args:
        entry_date: Buy-point date. Defaults to `2026-06-15`, the first trading
            date used by the deeper walk-forward smoke test.
        as_of_date: Latest data date. Defaults to `2026-06-30`, matching the
            current local dataset ceiling.
        positions: Optional open positions from `_load_positions`. When omitted,
            the function loads current demo positions itself.

    Returns:
        Summary and per-symbol analysis rows. Values are gross unless the field
        name explicitly says `net`.
    """
    resolved_entry_date = _coerce_date(entry_date, DEFAULT_ANALYSIS_ENTRY_DATE)
    resolved_as_of_date = _coerce_date(as_of_date, DEFAULT_ANALYSIS_AS_OF_DATE)
    position_rows = positions if positions is not None else _load_positions()
    symbols = [str(row["symbol"]) for row in position_rows]
    price_points = _historical_price_points(symbols, resolved_entry_date, resolved_as_of_date)
    contexts = _context_rows(symbols, resolved_as_of_date)
    recommendations = _recommendation_rows(symbols, resolved_as_of_date)

    rows: list[dict[str, Any]] = []
    totals = {
        "entry_value": 0.0,
        "current_value": 0.0,
        "gross_pnl": 0.0,
        "net_if_closed_pnl": 0.0,
        "buy_fees": 0.0,
        "sell_fees_taxes": 0.0,
    }
    for position in position_rows:
        symbol = str(position["symbol"])
        quantity = int(position.get("quantity", 0) or 0)
        price_meta = price_points.get(symbol, {})
        entry_price = _safe_float(price_meta.get("entry_price"))
        current_price = _safe_float(price_meta.get("current_price"))
        entry_value = quantity * entry_price
        current_value = quantity * current_price
        gross_pnl = current_value - entry_value
        gross_return = gross_pnl / entry_value if entry_value > 0 else 0.0
        buy_fee = entry_value * BUY_FEE_RATE
        sell_fee_tax = current_value * (SELL_FEE_RATE + SELL_TAX_RATE)
        net_if_closed_pnl = current_value - sell_fee_tax - entry_value - buy_fee
        net_if_closed_return = net_if_closed_pnl / (entry_value + buy_fee) if entry_value > 0 else 0.0

        rec = recommendations.get(symbol, {})
        ctx = contexts.get(symbol, {})
        rationale = rec.get("rationale") or {}
        symbol_rationale = rationale.get("symbol") if isinstance(rationale.get("symbol"), dict) else {}
        component_scores = symbol_rationale.get("component_scores") if isinstance(symbol_rationale.get("component_scores"), dict) else {}
        recommendation = str(rec.get("recommendation") or ctx.get("agent_candidate_state") or "watch")
        score = _safe_float(rec.get("score"), default=0.0)
        confidence = _safe_float(rec.get("confidence"), default=0.0)
        risk_level = str(rec.get("risk_level") or "unknown")
        trend_state = str(ctx.get("trend_state") or symbol_rationale.get("trend_state") or "neutral")
        liquidity_state = str(ctx.get("liquidity_state") or "unknown")
        return_5d = _safe_float(ctx.get("return_5d"))
        return_20d = _safe_float(ctx.get("return_20d"))
        relative_strength_20d = _safe_float(ctx.get("relative_strength_20d"))
        action = _suggest_portfolio_action(
            recommendation=recommendation,
            risk_level=risk_level,
            trend_state=trend_state,
            gross_return=gross_return,
            return_5d=return_5d,
            score=score,
        )
        reasons = _analysis_reasons(
            symbol=symbol,
            recommendation=recommendation,
            score=score,
            confidence=confidence,
            risk_level=risk_level,
            trend_state=trend_state,
            liquidity_state=liquidity_state,
            gross_return=gross_return,
            return_5d=return_5d,
            return_20d=return_20d,
            relative_strength_20d=relative_strength_20d,
            component_scores=component_scores,
            action=action,
        )
        rows.append(
            {
                "symbol": symbol,
                "quantity": quantity,
                "t0_quantity": int(position.get("t0_quantity", 0) or 0),
                "t1_quantity": int(position.get("t1_quantity", 0) or 0),
                "t2_quantity": int(position.get("t2_quantity", 0) or 0),
                "sellable_quantity": int(position.get("sellable_quantity", 0) or 0),
                "entry_date": str(price_meta.get("entry_date") or resolved_entry_date),
                "entry_price": entry_price,
                "current_date": str(price_meta.get("current_date") or resolved_as_of_date),
                "current_price": current_price,
                "entry_value": entry_value,
                "current_value": current_value,
                "gross_pnl": gross_pnl,
                "gross_return": gross_return,
                "net_if_closed_pnl": net_if_closed_pnl,
                "net_if_closed_return": net_if_closed_return,
                "return_bucket": _trend_bucket(gross_return),
                "action": action,
                "recommendation": recommendation,
                "score": score,
                "confidence": confidence,
                "risk_level": risk_level,
                "trend_state": trend_state,
                "liquidity_state": liquidity_state,
                "return_1d": _safe_float(ctx.get("return_1d")),
                "return_5d": return_5d,
                "return_20d": return_20d,
                "return_60d": _safe_float(ctx.get("return_60d")),
                "volatility_20d": _safe_float(ctx.get("volatility_20d")),
                "volatility_60d": _safe_float(ctx.get("volatility_60d")),
                "drawdown_from_peak": _safe_float(ctx.get("drawdown_from_peak")),
                "relative_strength_20d": relative_strength_20d,
                "relative_strength_60d": _safe_float(ctx.get("relative_strength_60d")),
                "traded_value_percentile": _safe_float(ctx.get("traded_value_cross_section_percentile")),
                "component_scores": component_scores,
                "reasons": reasons,
            }
        )
        totals["entry_value"] += entry_value
        totals["current_value"] += current_value
        totals["gross_pnl"] += gross_pnl
        totals["net_if_closed_pnl"] += net_if_closed_pnl
        totals["buy_fees"] += buy_fee
        totals["sell_fees_taxes"] += sell_fee_tax

    rows = sorted(rows, key=lambda item: item["gross_pnl"], reverse=True)
    summary = {
        **totals,
        "entry_date": resolved_entry_date.isoformat(),
        "as_of_date": resolved_as_of_date.isoformat(),
        "gross_return": totals["gross_pnl"] / totals["entry_value"] if totals["entry_value"] > 0 else 0.0,
        "net_if_closed_return": totals["net_if_closed_pnl"] / (totals["entry_value"] + totals["buy_fees"])
        if totals["entry_value"] > 0
        else 0.0,
        "winners": sum(1 for row in rows if row["gross_pnl"] > 0),
        "losers": sum(1 for row in rows if row["gross_pnl"] < 0),
        "neutral": sum(1 for row in rows if row["gross_pnl"] == 0),
    }
    return {"summary": summary, "rows": rows}


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


def load_demo_trading_snapshot(
    *,
    analysis_entry_date: date | str | None = None,
    analysis_as_of_date: date | str | None = None,
) -> dict[str, Any]:
    """Load account, positions, daily plan, trade history, and analysis state.

    Args:
        analysis_entry_date: Optional buy-point date for portfolio PnL analysis.
        analysis_as_of_date: Optional current-data date for portfolio PnL
            analysis.

    Returns:
        Snapshot payload consumed by the Demo Trading page.
    """
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
    trade_history = _load_trade_history()
    portfolio_analysis = load_demo_portfolio_analysis(
        entry_date=analysis_entry_date,
        as_of_date=analysis_as_of_date,
        positions=positions,
    )
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
        "orders": trade_history,
        "trade_history": trade_history,
        "portfolio_analysis": portfolio_analysis,
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


def _load_trade_history(limit: int = 200) -> list[dict[str, Any]]:
    """Load demo trading history for BUY, SELL, and seed/import events.

    Args:
        limit: Maximum number of most-recent ledger events to return.

    Returns:
        Display-ready trade events ordered from newest to oldest. The source is
        `v_demo_trading_trade_history`, which projects the append-only order
        ledger into a query-friendly history table.
    """
    with get_connection(read_only=True) as conn:
        rows = conn.execute(
            """
            SELECT trade_time, action, symbol, quantity, price, gross_amount,
                   fees, taxes, net_amount, realized_pnl, status,
                   settlement_date, settlement_ts, source, recommendation
            FROM v_demo_trading_trade_history
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
            "settlement_date": str(row[11]) if row[11] else "",
            "settlement_ts": str(row[12]) if row[12] else "",
            "source": row[13],
            "recommendation": row[14],
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
