"""Walk-forward backtest for BQuant VLLM decisions with TA and QAOA signals."""

from __future__ import annotations

import argparse
import json
import math
import re
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pandas as pd
import yaml

from agents.knowledge_base import retrieve_knowledge_chunks
from agents.llm_client import chat_completion
from agents.portfolio_optimizers.qaoa_optimizer import run_qaoa_optimizer
from agents.portfolio_optimizers.portfolio_features import load_qaoa_config
from pipelines.live_update_runtime import is_trading_day
from warehouse.duckdb_connection import get_connection


REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = REPO_ROOT / "configs" / "llm_backtest.yaml"
PRICE_VND_MULTIPLIER = 1000.0
PRICE_ALREADY_VND_THRESHOLD = 1000.0
ALLOWED_ACTIONS = {"BUY", "HOLD", "SELL", "TRIM", "WATCH"}


@dataclass
class SimLot:
    """One simulated portfolio lot."""

    symbol: str
    quantity: int
    avg_cost: float
    trade_date: date
    settlement_date: date


@dataclass
class SimPortfolio:
    """Simulated paper portfolio state for one backtest run."""

    cash: float
    lots: list[SimLot] = field(default_factory=list)

    def quantity(self, symbol: str) -> int:
        """Return current total quantity for a symbol."""
        return sum(lot.quantity for lot in self.lots if lot.symbol == symbol and lot.quantity > 0)

    def sellable_quantity(self, symbol: str, trade_date: date) -> int:
        """Return quantity sellable at the daily rebalance open under T+2.5."""
        return sum(
            lot.quantity
            for lot in self.lots
            if lot.symbol == symbol and lot.quantity > 0 and lot.settlement_date < trade_date
        )

    def settlement_buckets(self, symbol: str, trade_date: date) -> dict[str, int]:
        """Return T+0/T+1/T+2/sellable quantities for one symbol."""
        buckets = {"t0_quantity": 0, "t1_quantity": 0, "t2_quantity": 0, "sellable_quantity": 0}
        for lot in self.lots:
            if lot.symbol != symbol or lot.quantity <= 0:
                continue
            if lot.settlement_date < trade_date:
                buckets["sellable_quantity"] += lot.quantity
                continue
            elapsed = _trading_days_elapsed(lot.trade_date, trade_date)
            if elapsed <= 0:
                buckets["t0_quantity"] += lot.quantity
            elif elapsed == 1:
                buckets["t1_quantity"] += lot.quantity
            else:
                buckets["t2_quantity"] += lot.quantity
        return buckets

    def symbols(self) -> list[str]:
        """Return held symbols."""
        return sorted({lot.symbol for lot in self.lots if lot.quantity > 0})


def load_config(path: Path = CONFIG_PATH) -> dict[str, Any]:
    """Load backtest config with defaults."""
    with path.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def ensure_backtest_tables() -> None:
    """Create LLM backtest tables if warehouse bootstrap has not run."""
    with get_connection(read_only=False) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS llm_backtest_runs (
                run_id VARCHAR PRIMARY KEY,
                preset VARCHAR,
                start_date DATE NOT NULL,
                end_date DATE NOT NULL,
                status VARCHAR NOT NULL,
                model VARCHAR,
                mode VARCHAR NOT NULL,
                config_json VARCHAR NOT NULL,
                report_path VARCHAR,
                error_message VARCHAR,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS llm_backtest_prompt_events (
                event_id VARCHAR PRIMARY KEY,
                run_id VARCHAR NOT NULL,
                as_of_date DATE NOT NULL,
                trade_date DATE NOT NULL,
                attempt INTEGER NOT NULL,
                prompt_json VARCHAR NOT NULL,
                raw_response VARCHAR,
                parsed_response_json VARCHAR,
                validation_status VARCHAR NOT NULL,
                validation_errors_json VARCHAR NOT NULL,
                latency_seconds DOUBLE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS llm_backtest_decisions (
                run_id VARCHAR NOT NULL,
                trade_date DATE NOT NULL,
                as_of_date DATE NOT NULL,
                symbol VARCHAR NOT NULL,
                action VARCHAR NOT NULL,
                target_weight DOUBLE NOT NULL,
                confidence DOUBLE,
                risk_level VARCHAR,
                qaoa_selected BOOLEAN NOT NULL DEFAULT FALSE,
                validation_status VARCHAR NOT NULL,
                rationale VARCHAR,
                raw_json VARCHAR NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS llm_backtest_orders (
                order_id VARCHAR PRIMARY KEY,
                run_id VARCHAR NOT NULL,
                trade_date DATE NOT NULL,
                symbol VARCHAR NOT NULL,
                action VARCHAR NOT NULL,
                quantity BIGINT NOT NULL,
                price DOUBLE NOT NULL,
                gross_amount DOUBLE NOT NULL,
                fees DOUBLE NOT NULL,
                taxes DOUBLE NOT NULL,
                net_amount DOUBLE NOT NULL,
                status VARCHAR NOT NULL,
                reason VARCHAR,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS llm_backtest_positions (
                run_id VARCHAR NOT NULL,
                trade_date DATE NOT NULL,
                symbol VARCHAR NOT NULL,
                quantity BIGINT NOT NULL,
                sellable_quantity BIGINT NOT NULL,
                avg_cost DOUBLE,
                close_price DOUBLE,
                market_value DOUBLE NOT NULL,
                unrealized_pnl DOUBLE NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS llm_backtest_daily_nav (
                run_id VARCHAR NOT NULL,
                trade_date DATE NOT NULL,
                cash DOUBLE NOT NULL,
                market_value DOUBLE NOT NULL,
                nav DOUBLE NOT NULL,
                daily_return DOUBLE,
                drawdown DOUBLE,
                exposure DOUBLE,
                qaoa_agreement DOUBLE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (run_id, trade_date)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS llm_backtest_metrics (
                run_id VARCHAR PRIMARY KEY,
                total_return DOUBLE,
                annualized_return DOUBLE,
                volatility DOUBLE,
                sharpe DOUBLE,
                max_drawdown DOUBLE,
                win_rate DOUBLE,
                turnover DOUBLE,
                transaction_cost DOUBLE,
                invalid_response_rate DOUBLE,
                rejected_order_count INTEGER,
                qaoa_agreement_avg DOUBLE,
                benchmark_json VARCHAR NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )


def _to_vnd_price(value: Any) -> float:
    """Normalize HOSE quote values to VND/share."""
    price = float(value or 0.0)
    return price * PRICE_VND_MULTIPLIER if 0 < price < PRICE_ALREADY_VND_THRESHOLD else price


def _floor_lot(quantity: float, board_lot: int) -> int:
    """Round quantity down to board-lot size."""
    if quantity <= 0:
        return 0
    return int(math.floor(float(quantity) / board_lot) * board_lot)


def _trading_dates(start_date: date, end_date: date) -> list[date]:
    """Load exchange trading dates from market index data."""
    with get_connection(read_only=True) as conn:
        rows = conn.execute(
            """
            SELECT trading_date
            FROM market_index_daily_base
            WHERE symbol = 'VNINDEX'
              AND trading_date BETWEEN ? AND ?
            ORDER BY trading_date
            """,
            [start_date, end_date],
        ).fetchall()
    return [pd.Timestamp(row[0]).date() for row in rows if is_trading_day(pd.Timestamp(row[0]).date())]


def _resolve_date_range(args: argparse.Namespace, config: dict[str, Any]) -> tuple[date, date]:
    """Resolve explicit or preset backtest date range."""
    if args.start_date and args.end_date:
        return pd.Timestamp(args.start_date).date(), pd.Timestamp(args.end_date).date()
    preset = args.preset or "smoke"
    preset_cfg = (config.get("presets") or {}).get(preset, {})
    with get_connection(read_only=True) as conn:
        latest = pd.Timestamp(conn.execute("SELECT max(trading_date) FROM market_index_daily_base").fetchone()[0]).date()
    if preset_cfg.get("trailing_trading_days"):
        dates = _trading_dates(date(2016, 1, 1), latest)
        tail = dates[-int(preset_cfg["trailing_trading_days"]):]
        return tail[0], tail[-1]
    start = pd.Timestamp(preset_cfg.get("start_date", args.start_date or "2023-01-01")).date()
    end_raw = preset_cfg.get("end_date", "latest")
    end = latest if end_raw == "latest" else pd.Timestamp(end_raw).date()
    return start, end


def _add_trading_days(anchor: date, count: int) -> date:
    """Advance by configured trading days."""
    current = anchor
    remaining = count
    while remaining > 0:
        current += pd.Timedelta(days=1).to_pytimedelta()
        if is_trading_day(current):
            remaining -= 1
    return current


def _trading_days_elapsed(start: date, end: date) -> int:
    """Count trading days from a lot trade date to a later rebalance date."""
    if end <= start:
        return 0
    current = start
    elapsed = 0
    while current < end:
        current += pd.Timedelta(days=1).to_pytimedelta()
        if is_trading_day(current):
            elapsed += 1
    return elapsed


def _previous_date(dates: list[date], current: date) -> date | None:
    """Return previous date from a sorted trading-date list."""
    idx = dates.index(current)
    return dates[idx - 1] if idx > 0 else None


def _price_map(trade_date: date) -> dict[str, dict[str, float]]:
    """Load open/close prices for all VN30 symbols on one date."""
    with get_connection(read_only=True) as conn:
        rows = conn.execute(
            """
            SELECT symbol, open, close
            FROM daily_ohlcv_base
            WHERE trading_date = ?
            """,
            [trade_date],
        ).fetchall()
    return {
        str(symbol): {"open": _to_vnd_price(open_price), "close": _to_vnd_price(close_price)}
        for symbol, open_price, close_price in rows
    }


def _current_weights(portfolio: SimPortfolio, prices: dict[str, dict[str, float]]) -> dict[str, float]:
    """Compute current symbol weights from open prices."""
    values = {
        symbol: portfolio.quantity(symbol) * prices.get(symbol, {}).get("open", 0.0)
        for symbol in portfolio.symbols()
    }
    nav = portfolio.cash + sum(values.values())
    return {symbol: value / nav for symbol, value in values.items() if nav > 0 and value > 0}


def _portfolio_context(portfolio: SimPortfolio, trade_date: date, prices: dict[str, dict[str, float]]) -> dict[str, Any]:
    """Build JSON-safe portfolio context for the LLM."""
    positions = []
    market_value = 0.0
    for symbol in portfolio.symbols():
        quantity = portfolio.quantity(symbol)
        price = prices.get(symbol, {}).get("open", 0.0)
        value = quantity * price
        market_value += value
        buckets = portfolio.settlement_buckets(symbol, trade_date)
        positions.append(
            {
                "symbol": symbol,
                "quantity": quantity,
                **buckets,
                "open_price": price,
                "market_value": value,
            }
        )
    nav = portfolio.cash + market_value
    return {"cash": portfolio.cash, "market_value": market_value, "nav": nav, "positions": positions}


def _market_context(as_of_date: date) -> dict[str, Any]:
    """Load market regime context for one as-of date."""
    with get_connection(read_only=True) as conn:
        row = conn.execute(
            """
            SELECT *
            FROM analytics_marts.mart_market_regime_daily
            WHERE trading_date = ?
            """,
            [as_of_date],
        ).fetchdf()
    if row.empty:
        return {}
    record = _json_safe_record(row.iloc[0].to_dict())
    return {
        key: _compact_value(record.get(key))
        for key in [
            "trading_date",
            "market_regime",
            "volatility_regime",
            "regime_score",
            "vnindex_close",
            "vnindex_return_1d",
            "vnindex_return_20d",
            "vnindex_volatility_20d",
            "vn30_close",
            "vn30_return_1d",
            "vn30_return_20d",
            "vn30_volatility_20d",
            "active_symbols",
            "advancers",
            "decliners",
            "advancer_ratio",
            "breadth_score",
        ]
        if key in record
    }


def _ta_context(as_of_date: date, limit: int) -> list[dict[str, Any]]:
    """Load top TA signal rows for one as-of date."""
    with get_connection(read_only=True) as conn:
        frame = conn.execute(
            """
            SELECT
                symbol, trading_date, close, return_5d, return_20d, return_60d,
                volatility_20d, relative_strength_20d, trend_state, liquidity_state,
                ta_trend_score, ta_momentum_score, ta_volatility_score,
                ta_liquidity_score, ta_relative_strength_score, ta_composite_score,
                ta_action_bias, ta_risk_flag, rsi_14, macd_histogram, adx_14,
                atr_pct_14, volume_zscore_20, mfi_14
            FROM analytics_marts.mart_agent_context_daily
            WHERE trading_date = ?
              AND data_quality_status = 'pass'
              AND ta_composite_score IS NOT NULL
            ORDER BY ta_composite_score DESC, traded_value_cross_section_percentile DESC
            LIMIT ?
            """,
            [as_of_date, int(limit)],
        ).df()
    return [_compact_signal_record(_json_safe_record(row.to_dict())) for _, row in frame.iterrows()]


def _json_safe_record(record: dict[str, Any]) -> dict[str, Any]:
    """Convert pandas/numpy scalars to JSON-safe values."""
    safe: dict[str, Any] = {}
    for key, value in record.items():
        if value is None:
            safe[key] = None
        elif isinstance(value, (datetime, date, pd.Timestamp)):
            safe[key] = pd.Timestamp(value).date().isoformat()
        elif isinstance(value, float) and not math.isfinite(value):
            safe[key] = None
        elif pd.isna(value):
            safe[key] = None
        elif hasattr(value, "item"):
            safe[key] = value.item()
        else:
            safe[key] = value
    return safe


def _compact_value(value: Any) -> Any:
    """Round numeric values before they enter the local-LLM prompt."""
    if isinstance(value, float):
        return round(value, 4)
    return value


def _compact_signal_record(record: dict[str, Any]) -> dict[str, Any]:
    """Keep prompt-critical TA fields with rounded numeric values."""
    fields = [
        "symbol",
        "close",
        "return_5d",
        "return_20d",
        "return_60d",
        "volatility_20d",
        "relative_strength_20d",
        "trend_state",
        "liquidity_state",
        "ta_trend_score",
        "ta_momentum_score",
        "ta_volatility_score",
        "ta_liquidity_score",
        "ta_relative_strength_score",
        "ta_composite_score",
        "ta_action_bias",
        "ta_risk_flag",
        "rsi_14",
        "macd_histogram",
        "adx_14",
        "atr_pct_14",
        "volume_zscore_20",
        "mfi_14",
    ]
    return {key: _compact_value(record.get(key)) for key in fields if key in record}


def _build_prompt_context(
    *,
    as_of_date: date,
    trade_date: date,
    portfolio: SimPortfolio,
    prices: dict[str, dict[str, float]],
    qaoa_signal: dict[str, Any],
    config: dict[str, Any],
) -> dict[str, Any]:
    """Build the date-safe context passed to VLLM."""
    context_cfg = config.get("context", {})
    skill_context = _compact_knowledge_context(
        str(context_cfg.get("skill_query", "")),
        limit=int(context_cfg.get("skill_limit", 5)),
        max_total_chars=int(context_cfg.get("skill_max_total_chars", 8000)),
    )
    return {
        "task": "daily_vn30_portfolio_decision",
        "rules": {
            "data_cutoff": as_of_date.isoformat(),
            "trade_date": trade_date.isoformat(),
            "max_positions": config["portfolio"]["max_positions"],
            "max_symbol_weight": config["portfolio"]["max_symbol_weight"],
            "min_cash_weight": config["portfolio"]["min_cash_weight"],
            "board_lot": config["portfolio"]["board_lot"],
            "settlement_rule": config["portfolio"]["settlement_rule"],
        },
        "market_context": _market_context(as_of_date),
        "ta_signals": _ta_context(as_of_date, int(context_cfg.get("candidate_limit", 12))),
        "qaoa_signal": {
            "run_id": qaoa_signal.get("run_id"),
            "backend": qaoa_signal.get("backend"),
            "fallback_used": qaoa_signal.get("fallback_used"),
            "selected_symbols": qaoa_signal.get("selected_symbols"),
            "proposed_weights": {
                symbol: round(float(weight), 4)
                for symbol, weight in (qaoa_signal.get("proposed_weights") or {}).items()
            },
            "energy": _compact_value(qaoa_signal.get("energy")),
            "candidates": _compact_qaoa_candidates(
                qaoa_signal.get("candidates") or [],
                limit=int(context_cfg.get("candidate_limit", 12)),
            ),
        },
        "portfolio": _portfolio_context(portfolio, trade_date, prices),
        "knowledge": skill_context,
    }


def _compact_knowledge_context(question: str, *, limit: int, max_total_chars: int) -> list[dict[str, Any]]:
    """Retrieve compact playbook snippets for an 8k-context local LLM."""
    chunks = retrieve_knowledge_chunks(question, limit=limit, max_total_chars=max_total_chars)
    snippets: list[dict[str, Any]] = []
    remaining = max_total_chars
    for chunk in chunks:
        if remaining <= 0:
            break
        excerpt = chunk.content[: min(500, remaining)].strip()
        remaining -= len(excerpt)
        snippets.append(
            {
                "source_path": chunk.source_path,
                "source_type": chunk.source_type,
                "title": chunk.title,
                "excerpt": excerpt,
            }
        )
    return snippets


def _compact_qaoa_candidates(candidates: list[dict[str, Any]], *, limit: int) -> list[dict[str, Any]]:
    """Keep only prompt-critical QAOA candidate fields."""
    fields = [
        "symbol",
        "expected_alpha",
        "ta_composite_score",
        "ta_action_bias",
        "ta_risk_flag",
        "return_20d",
        "relative_strength_20d",
        "volatility_20d",
        "traded_value_cross_section_percentile",
    ]
    return [
        {key: _compact_value(row.get(key)) for key in fields}
        for row in candidates[:limit]
    ]


def _system_prompt() -> str:
    """Return the strict VLLM system prompt."""
    return (
        "You are BQuant's VN30 portfolio decision agent. Use only the supplied context. "
        "Do not invent prices, dates, or symbols. Return one JSON object only. "
        "You may use QAOA as an optimizer signal, not an oracle. Explain overrides with TA/risk evidence. "
        "SELL or TRIM is forbidden when the supplied sellable_quantity for that symbol is 0."
    )


def _user_prompt(context: dict[str, Any]) -> str:
    """Return one prompt with the required JSON schema."""
    return (
        "Create today's VN30 demo portfolio decision from this context. "
        "Respect max positions, cash, board lots, and sellable quantities. "
        "For T+2.5, use SELL/TRIM only when the symbol is currently held and sellable_quantity > 0; "
        "otherwise use HOLD/WATCH and explain the settlement lock. "
        "Keep the JSON compact. Use short string values for ta_summary, qaoa_summary, rationale, "
        "portfolio_notes, and risk_warnings; do not use nested objects in these fields. "
        "Return JSON only. Required keys: as_of_date, trade_date, market_view, risk_mode, "
        "qaoa_agreement, cash_weight, positions, portfolio_notes, risk_warnings. "
        "Allowed risk_mode values: risk_on, balanced, risk_off. "
        "Allowed qaoa_agreement values: follow, override, partial. "
        "Each position requires: symbol, action, target_weight, confidence, risk_level, "
        "ta_summary, qaoa_summary, rationale. Allowed actions: BUY,HOLD,SELL,TRIM,WATCH. "
        f"as_of_date must be {context['rules']['data_cutoff']}; "
        f"trade_date must be {context['rules']['trade_date']}.\n\n"
        f"CONTEXT:\n{json.dumps(context, ensure_ascii=False, default=str)}"
    )


def _mock_llm_decision(context: dict[str, Any]) -> dict[str, Any]:
    """Generate deterministic mock decisions from QAOA output for smoke tests."""
    qaoa_weights = context["qaoa_signal"].get("proposed_weights") or {}
    held_rows = {row["symbol"]: row for row in context["portfolio"]["positions"]}
    held = set(held_rows)
    selected = set(qaoa_weights)
    positions = []
    for symbol, weight in sorted(qaoa_weights.items(), key=lambda item: item[1], reverse=True):
        positions.append(
            {
                "symbol": symbol,
                "action": "HOLD" if symbol in held else "BUY",
                "target_weight": float(weight),
                "confidence": 0.70,
                "risk_level": "medium",
                "ta_summary": "mock follows TA composite score from context",
                "qaoa_summary": "mock follows QAOA proposed weight",
                "rationale": "QAOA-selected symbol with positive TA signal.",
            }
        )
    for symbol in sorted(held - selected):
        row = held_rows[symbol]
        can_sell = int(row.get("sellable_quantity", 0) or 0) > 0
        current_weight = (
            float(row.get("market_value", 0.0) or 0.0) / float(context["portfolio"].get("nav", 1.0) or 1.0)
        )
        positions.append(
            {
                "symbol": symbol,
                "action": "SELL" if can_sell else "HOLD",
                "target_weight": 0.0 if can_sell else min(current_weight, context["rules"]["max_symbol_weight"]),
                "confidence": 0.65,
                "risk_level": "medium",
                "ta_summary": "mock exits only when settlement permits selling",
                "qaoa_summary": "not selected by QAOA",
                "rationale": (
                    "Symbol dropped from optimizer basket."
                    if can_sell
                    else "Symbol dropped from optimizer basket but is not sellable under T+2.5, so it is held."
                ),
            }
        )
    return {
        "as_of_date": context["rules"]["data_cutoff"],
        "trade_date": context["rules"]["trade_date"],
        "market_view": "neutral",
        "risk_mode": "balanced",
        "qaoa_agreement": "follow",
        "cash_weight": 0.05,
        "positions": positions,
        "portfolio_notes": "Mock decision for deterministic smoke testing.",
        "risk_warnings": [],
    }


def _call_vllm(context: dict[str, Any], config: dict[str, Any]) -> tuple[str, float | None]:
    """Call the local VLLM runtime and return raw text plus latency."""
    response = chat_completion(
        [
            {"role": "system", "content": _system_prompt()},
            {"role": "user", "content": _user_prompt(context)},
        ],
        max_tokens=int(config.get("runtime", {}).get("max_tokens", 768)),
        temperature=float(config.get("runtime", {}).get("temperature", 0.1)),
    )
    return response.text, response.latency_seconds


def _parse_json_response(text: str) -> dict[str, Any]:
    """Parse a JSON object from VLLM text."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?", "", cleaned).strip()
        cleaned = re.sub(r"```$", "", cleaned).strip()
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
        if not match:
            raise
        parsed = json.loads(match.group(0))
    if not isinstance(parsed, dict):
        raise ValueError("VLLM response must be a JSON object")
    return parsed


def _validate_decision(
    decision: dict[str, Any],
    *,
    context: dict[str, Any],
    portfolio: SimPortfolio,
    trade_date: date,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Validate and sanitize a VLLM decision object."""
    errors: list[str] = []
    as_of_date = context["rules"]["data_cutoff"]
    allowed_symbols = {row["symbol"] for row in context["ta_signals"]}
    allowed_symbols.update(str(row.get("symbol", "")).upper() for row in context["qaoa_signal"].get("candidates") or [])
    allowed_symbols.update(str(symbol).upper() for symbol in context["qaoa_signal"].get("selected_symbols") or [])
    allowed_symbols.update(str(symbol).upper() for symbol in (context["qaoa_signal"].get("proposed_weights") or {}).keys())
    allowed_symbols.update(portfolio.symbols())
    max_positions = int(context["rules"]["max_positions"])
    max_weight = float(context["rules"]["max_symbol_weight"])
    min_cash = float(context["rules"]["min_cash_weight"])
    if str(decision.get("as_of_date")) != as_of_date:
        errors.append("as_of_date does not match context cutoff")
    if float(decision.get("cash_weight", 0.0) or 0.0) < min_cash:
        errors.append("cash_weight below minimum")
    rows: list[dict[str, Any]] = []
    total_weight = float(decision.get("cash_weight", 0.0) or 0.0)
    future_dates = []
    for raw in decision.get("positions") or []:
        symbol = str(raw.get("symbol", "")).upper().strip()
        action = str(raw.get("action", "WATCH")).upper().strip()
        weight = float(raw.get("target_weight", 0.0) or 0.0)
        if symbol not in allowed_symbols and symbol not in portfolio.symbols():
            errors.append(f"{symbol} is not an allowed VN30/context symbol")
            continue
        if action not in ALLOWED_ACTIONS:
            errors.append(f"{symbol} has invalid action {action}")
            action = "WATCH"
        if weight > max_weight:
            errors.append(f"{symbol} target_weight exceeds max_symbol_weight")
            weight = max_weight
        if action in {"SELL", "TRIM"} and portfolio.sellable_quantity(symbol, trade_date) <= 0:
            errors.append(f"{symbol} sell requested without sellable quantity")
        text_blob = " ".join(str(raw.get(key, "")) for key in ["ta_summary", "qaoa_summary", "rationale"])
        future_dates.extend([value for value in re.findall(r"20\\d{2}-\\d{2}-\\d{2}", text_blob) if value > as_of_date])
        rows.append(
            {
                "symbol": symbol,
                "action": action,
                "target_weight": max(0.0, weight),
                "confidence": float(raw.get("confidence", 0.0) or 0.0),
                "risk_level": str(raw.get("risk_level", "")),
                "rationale": str(raw.get("rationale", "")),
                "raw": raw,
            }
        )
        if action in {"BUY", "HOLD"}:
            total_weight += max(0.0, weight)
    active_count = sum(1 for row in rows if row["target_weight"] > 0)
    if active_count > max_positions:
        errors.append("active positions exceed max_positions")
        rows = sorted(rows, key=lambda row: row["target_weight"], reverse=True)
        kept = 0
        for row in rows:
            if row["target_weight"] > 0:
                kept += 1
                if kept > max_positions:
                    row["target_weight"] = 0.0
                    row["action"] = "WATCH"
    if total_weight > 1.000001:
        errors.append("target weights plus cash exceed 100%")
    if future_dates:
        errors.append(f"rationale contains future dates: {sorted(set(future_dates))[:3]}")
    return rows, errors


def _persist_prompt_event(
    *,
    run_id: str,
    as_of_date: date,
    trade_date: date,
    attempt: int,
    prompt_context: dict[str, Any],
    raw_response: str,
    parsed: dict[str, Any] | None,
    validation_errors: list[str],
    latency: float | None,
) -> None:
    """Persist one LLM prompt/response event."""
    with get_connection(read_only=False) as conn:
        conn.execute(
            """
            INSERT INTO llm_backtest_prompt_events (
                event_id, run_id, as_of_date, trade_date, attempt, prompt_json,
                raw_response, parsed_response_json, validation_status,
                validation_errors_json, latency_seconds
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                str(uuid.uuid4()),
                run_id,
                as_of_date,
                trade_date,
                attempt,
                json.dumps(prompt_context, ensure_ascii=False, default=str),
                raw_response,
                json.dumps(parsed or {}, ensure_ascii=False, default=str),
                "valid" if not validation_errors else "invalid",
                json.dumps(validation_errors, ensure_ascii=False),
                latency,
            ],
        )


def _execute_rebalance(
    *,
    run_id: str,
    portfolio: SimPortfolio,
    decisions: list[dict[str, Any]],
    qaoa_signal: dict[str, Any],
    trade_date: date,
    prices: dict[str, dict[str, float]],
    config: dict[str, Any],
) -> tuple[list[dict[str, Any]], float]:
    """Execute target weights at trade-date open and persist order rows."""
    cfg = config["portfolio"]
    board_lot = int(cfg["board_lot"])
    buy_fee = float(cfg["buy_fee_rate"])
    sell_fee = float(cfg["sell_fee_rate"])
    sell_tax = float(cfg["sell_tax_rate"])
    slippage = float(cfg["slippage_rate"])
    nav_open = portfolio.cash + sum(portfolio.quantity(symbol) * prices.get(symbol, {}).get("open", 0.0) for symbol in portfolio.symbols())
    target_by_symbol = {row["symbol"]: row["target_weight"] for row in decisions if row["target_weight"] > 0}
    order_rows: list[dict[str, Any]] = []

    def record_order(row: dict[str, Any]) -> None:
        order_rows.append(row)
        with get_connection(read_only=False) as conn:
            conn.execute(
                """
                INSERT INTO llm_backtest_orders (
                    order_id, run_id, trade_date, symbol, action, quantity, price,
                    gross_amount, fees, taxes, net_amount, status, reason
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    row["order_id"], run_id, trade_date, row["symbol"], row["action"],
                    row["quantity"], row["price"], row["gross_amount"], row["fees"],
                    row["taxes"], row["net_amount"], row["status"], row.get("reason"),
                ],
            )

    for symbol in sorted(set(portfolio.symbols()) | set(target_by_symbol)):
        open_price = prices.get(symbol, {}).get("open", 0.0)
        if open_price <= 0:
            continue
        current_qty = portfolio.quantity(symbol)
        target_qty = _floor_lot((target_by_symbol.get(symbol, 0.0) * nav_open) / open_price, board_lot)
        sell_qty = _floor_lot(max(current_qty - target_qty, 0), board_lot)
        if sell_qty <= 0:
            continue
        available = portfolio.sellable_quantity(symbol, trade_date)
        fill_qty = min(sell_qty, _floor_lot(available, board_lot))
        if fill_qty <= 0:
            record_order({
                "order_id": str(uuid.uuid4()), "symbol": symbol, "action": "SELL",
                "quantity": 0, "price": open_price, "gross_amount": 0.0, "fees": 0.0,
                "taxes": 0.0, "net_amount": 0.0, "status": "rejected",
                "reason": "no_sellable_quantity",
            })
            continue
        execution_price = open_price * (1.0 - slippage)
        gross = execution_price * fill_qty
        fees = gross * sell_fee
        taxes = gross * sell_tax
        net = gross - fees - taxes
        remaining = fill_qty
        for lot in sorted(portfolio.lots, key=lambda item: (item.trade_date, item.symbol)):
            if remaining <= 0 or lot.symbol != symbol or lot.settlement_date >= trade_date or lot.quantity <= 0:
                continue
            used = min(lot.quantity, remaining)
            lot.quantity -= used
            remaining -= used
        portfolio.lots = [lot for lot in portfolio.lots if lot.quantity > 0]
        portfolio.cash += net
        record_order({
            "order_id": str(uuid.uuid4()), "symbol": symbol, "action": "SELL",
            "quantity": fill_qty, "price": execution_price, "gross_amount": gross,
            "fees": fees, "taxes": taxes, "net_amount": net, "status": "filled",
            "reason": None,
        })

    for symbol, target_weight in sorted(target_by_symbol.items(), key=lambda item: item[1], reverse=True):
        open_price = prices.get(symbol, {}).get("open", 0.0)
        if open_price <= 0:
            continue
        current_qty = portfolio.quantity(symbol)
        target_qty = _floor_lot((target_weight * nav_open) / open_price, board_lot)
        buy_qty = _floor_lot(max(target_qty - current_qty, 0), board_lot)
        if buy_qty <= 0:
            continue
        execution_price = open_price * (1.0 + slippage)
        max_affordable = _floor_lot(portfolio.cash / (execution_price * (1.0 + buy_fee)), board_lot)
        fill_qty = min(buy_qty, max_affordable)
        if fill_qty <= 0:
            record_order({
                "order_id": str(uuid.uuid4()), "symbol": symbol, "action": "BUY",
                "quantity": 0, "price": execution_price, "gross_amount": 0.0,
                "fees": 0.0, "taxes": 0.0, "net_amount": 0.0,
                "status": "rejected", "reason": "insufficient_cash",
            })
            continue
        gross = execution_price * fill_qty
        fees = gross * buy_fee
        net = gross + fees
        portfolio.cash -= net
        portfolio.lots.append(
            SimLot(
                symbol=symbol,
                quantity=fill_qty,
                avg_cost=net / fill_qty,
                trade_date=trade_date,
                settlement_date=_add_trading_days(trade_date, 2),
            )
        )
        record_order({
            "order_id": str(uuid.uuid4()), "symbol": symbol, "action": "BUY",
            "quantity": fill_qty, "price": execution_price, "gross_amount": gross,
            "fees": fees, "taxes": 0.0, "net_amount": net, "status": "filled",
            "reason": None,
        })
    turnover = sum(row["gross_amount"] for row in order_rows if row["status"] == "filled") / nav_open if nav_open > 0 else 0.0
    return order_rows, turnover


def _mark_to_market(
    *,
    run_id: str,
    portfolio: SimPortfolio,
    trade_date: date,
    prices: dict[str, dict[str, float]],
    previous_nav: float | None,
    high_watermark: float,
    qaoa_selected: set[str],
    llm_selected: set[str],
) -> tuple[float, float]:
    """Persist positions and NAV after market close."""
    market_value = 0.0
    with get_connection(read_only=False) as conn:
        for symbol in portfolio.symbols():
            quantity = portfolio.quantity(symbol)
            close_price = prices.get(symbol, {}).get("close", 0.0)
            value = quantity * close_price
            market_value += value
            cost_basis = sum(lot.quantity * lot.avg_cost for lot in portfolio.lots if lot.symbol == symbol)
            avg_cost = cost_basis / quantity if quantity else None
            conn.execute(
                """
                INSERT INTO llm_backtest_positions (
                    run_id, trade_date, symbol, quantity, sellable_quantity,
                    avg_cost, close_price, market_value, unrealized_pnl
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    run_id, trade_date, symbol, quantity,
                    portfolio.sellable_quantity(symbol, trade_date), avg_cost,
                    close_price, value, value - cost_basis,
                ],
            )
        nav = portfolio.cash + market_value
        daily_return = nav / previous_nav - 1.0 if previous_nav and previous_nav > 0 else None
        updated_hwm = max(high_watermark, nav)
        drawdown = nav / updated_hwm - 1.0 if updated_hwm > 0 else 0.0
        exposure = market_value / nav if nav > 0 else 0.0
        agreement = len(qaoa_selected & llm_selected) / len(qaoa_selected | llm_selected) if (qaoa_selected | llm_selected) else 1.0
        conn.execute(
            """
            INSERT INTO llm_backtest_daily_nav (
                run_id, trade_date, cash, market_value, nav, daily_return,
                drawdown, exposure, qaoa_agreement
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (run_id, trade_date) DO UPDATE SET
                cash = excluded.cash,
                market_value = excluded.market_value,
                nav = excluded.nav,
                daily_return = excluded.daily_return,
                drawdown = excluded.drawdown,
                exposure = excluded.exposure,
                qaoa_agreement = excluded.qaoa_agreement
            """,
            [run_id, trade_date, portfolio.cash, market_value, nav, daily_return, drawdown, exposure, agreement],
        )
    return nav, updated_hwm


def _persist_decisions(
    *,
    run_id: str,
    trade_date: date,
    as_of_date: date,
    decisions: list[dict[str, Any]],
    qaoa_selected: set[str],
    validation_errors: list[str],
) -> None:
    """Persist sanitized VLLM decisions."""
    status = "valid" if not validation_errors else "invalid_sanitized"
    with get_connection(read_only=False) as conn:
        for row in decisions:
            conn.execute(
                """
                INSERT INTO llm_backtest_decisions (
                    run_id, trade_date, as_of_date, symbol, action, target_weight,
                    confidence, risk_level, qaoa_selected, validation_status,
                    rationale, raw_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    run_id, trade_date, as_of_date, row["symbol"], row["action"],
                    row["target_weight"], row["confidence"], row["risk_level"],
                    row["symbol"] in qaoa_selected, status, row["rationale"],
                    json.dumps(row["raw"], ensure_ascii=False, default=str),
                ],
            )


def _compute_metrics(run_id: str, initial_cash: float, turnover_values: list[float], invalid_days: int, total_days: int) -> dict[str, Any]:
    """Compute and persist summary backtest metrics."""
    with get_connection(read_only=True) as conn:
        nav = conn.execute(
            "SELECT trade_date, nav, daily_return, drawdown, qaoa_agreement FROM llm_backtest_daily_nav WHERE run_id = ? ORDER BY trade_date",
            [run_id],
        ).df()
    if nav.empty:
        return {}
    returns = nav["daily_return"].dropna()
    final_nav = float(nav["nav"].iloc[-1])
    total_return = final_nav / initial_cash - 1.0
    annualized_return = (1.0 + total_return) ** (252.0 / max(len(nav), 1)) - 1.0
    volatility = float(returns.std(ddof=1) * math.sqrt(252.0)) if len(returns) > 1 else 0.0
    sharpe = annualized_return / volatility if volatility > 0 else 0.0
    max_drawdown = float(nav["drawdown"].min())
    win_rate = float((returns > 0).mean()) if len(returns) else 0.0
    transaction_cost = _transaction_cost(run_id)
    benchmarks = _benchmarks(pd.Timestamp(nav["trade_date"].min()).date(), pd.Timestamp(nav["trade_date"].max()).date())
    metrics = {
        "total_return": total_return,
        "annualized_return": annualized_return,
        "volatility": volatility,
        "sharpe": sharpe,
        "max_drawdown": max_drawdown,
        "win_rate": win_rate,
        "turnover": float(sum(turnover_values)),
        "transaction_cost": transaction_cost,
        "invalid_response_rate": invalid_days / total_days if total_days else 0.0,
        "rejected_order_count": _rejected_order_count(run_id),
        "qaoa_agreement_avg": float(nav["qaoa_agreement"].mean()),
        "benchmark_json": json.dumps(benchmarks, sort_keys=True),
    }
    with get_connection(read_only=False) as conn:
        conn.execute(
            """
            INSERT INTO llm_backtest_metrics (
                run_id, total_return, annualized_return, volatility, sharpe,
                max_drawdown, win_rate, turnover, transaction_cost,
                invalid_response_rate, rejected_order_count, qaoa_agreement_avg,
                benchmark_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (run_id) DO UPDATE SET
                total_return = excluded.total_return,
                annualized_return = excluded.annualized_return,
                volatility = excluded.volatility,
                sharpe = excluded.sharpe,
                max_drawdown = excluded.max_drawdown,
                win_rate = excluded.win_rate,
                turnover = excluded.turnover,
                transaction_cost = excluded.transaction_cost,
                invalid_response_rate = excluded.invalid_response_rate,
                rejected_order_count = excluded.rejected_order_count,
                qaoa_agreement_avg = excluded.qaoa_agreement_avg,
                benchmark_json = excluded.benchmark_json
            """,
            [
                run_id,
                metrics["total_return"],
                metrics["annualized_return"],
                metrics["volatility"],
                metrics["sharpe"],
                metrics["max_drawdown"],
                metrics["win_rate"],
                metrics["turnover"],
                metrics["transaction_cost"],
                metrics["invalid_response_rate"],
                metrics["rejected_order_count"],
                metrics["qaoa_agreement_avg"],
                metrics["benchmark_json"],
            ],
        )
    return metrics


def _transaction_cost(run_id: str) -> float:
    """Return total fees and taxes for a run."""
    with get_connection(read_only=True) as conn:
        value = conn.execute(
            "SELECT coalesce(sum(fees + taxes), 0) FROM llm_backtest_orders WHERE run_id = ? AND status = 'filled'",
            [run_id],
        ).fetchone()[0]
    return float(value or 0.0)


def _rejected_order_count(run_id: str) -> int:
    """Return rejected order count for a run."""
    with get_connection(read_only=True) as conn:
        value = conn.execute(
            "SELECT count(*) FROM llm_backtest_orders WHERE run_id = ? AND status = 'rejected'",
            [run_id],
        ).fetchone()[0]
    return int(value or 0)


def _benchmarks(start_date: date, end_date: date) -> dict[str, float | None]:
    """Compute simple benchmark returns over the same date window."""
    benchmarks: dict[str, float | None] = {}
    with get_connection(read_only=True) as conn:
        for symbol in ["VNINDEX", "VN30"]:
            row = conn.execute(
                """
                SELECT finish.close / nullif(start.open, 0) - 1
                FROM market_index_daily_base start
                INNER JOIN market_index_daily_base finish
                  ON start.symbol = finish.symbol
                WHERE start.symbol = ?
                  AND start.trading_date = ?
                  AND finish.trading_date = ?
                """,
                [symbol, start_date, end_date],
            ).fetchone()
            benchmarks[symbol] = float(row[0]) if row and row[0] is not None else None
        ew = conn.execute(
            """
            WITH start_px AS (
                SELECT symbol, open
                FROM daily_ohlcv_base
                WHERE trading_date = ?
            ),
            end_px AS (
                SELECT symbol, close
                FROM daily_ohlcv_base
                WHERE trading_date = ?
            )
            SELECT avg(end_px.close / nullif(start_px.open, 0) - 1)
            FROM start_px
            INNER JOIN end_px USING(symbol)
            """,
            [start_date, end_date],
        ).fetchone()
    benchmarks["equal_weight_vn30"] = float(ew[0]) if ew and ew[0] is not None else None
    return benchmarks


def _write_report(run_id: str, metrics: dict[str, Any], config: dict[str, Any], *, start_date: date, end_date: date) -> Path:
    """Write a Markdown backtest report."""
    output_dir = REPO_ROOT / str(config.get("reports", {}).get("output_dir", "agent_build_reports/llm_backtests"))
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"llm_backtest_{start_date}_{end_date}_{run_id[:8]}.md"
    benchmarks = json.loads(metrics.get("benchmark_json", "{}")) if metrics else {}
    lines = [
        "# BQuant VLLM + TA + QAOA Backtest",
        "",
        f"- Run ID: `{run_id}`",
        f"- Window: `{start_date}` to `{end_date}`",
        f"- Total return: `{metrics.get('total_return', 0):.2%}`",
        f"- Annualized return: `{metrics.get('annualized_return', 0):.2%}`",
        f"- Sharpe: `{metrics.get('sharpe', 0):.3f}`",
        f"- Max drawdown: `{metrics.get('max_drawdown', 0):.2%}`",
        f"- Win rate: `{metrics.get('win_rate', 0):.2%}`",
        f"- Turnover: `{metrics.get('turnover', 0):.2f}`",
        f"- Fees/taxes: `{metrics.get('transaction_cost', 0):,.0f} VND`",
        f"- Invalid response rate: `{metrics.get('invalid_response_rate', 0):.2%}`",
        f"- Rejected orders: `{metrics.get('rejected_order_count', 0)}`",
        f"- Avg QAOA/VLLM agreement: `{metrics.get('qaoa_agreement_avg', 0):.2%}`",
        "",
        "## Benchmarks",
        "",
        "| Benchmark | Return |",
        "|---|---:|",
    ]
    for name, value in benchmarks.items():
        lines.append(f"| {name} | {value:.2%} |" if value is not None else f"| {name} | N/A |")
    lines.extend(
        [
            "",
            "## Notes",
            "",
            "- VLLM receives TA signals, QAOA optimizer output, market regime, and portfolio settlement state.",
            "- QAOA is used as a signal to the LLM, not as an oracle.",
            "- The simulator does not mutate the live Demo Trading account.",
        ]
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def run_backtest(args: argparse.Namespace) -> dict[str, Any]:
    """Run the configured VLLM/QAOA walk-forward backtest."""
    ensure_backtest_tables()
    config = load_config()
    start_date, end_date = _resolve_date_range(args, config)
    dates = _trading_dates(start_date, end_date)
    if len(dates) < 2:
        raise RuntimeError("Backtest requires at least two trading dates")
    run_id = args.run_id or str(uuid.uuid4())
    mode = "mock_llm" if args.mock_llm else config.get("runtime", {}).get("mode", "real_llm")
    qaoa_config = load_qaoa_config()
    portfolio = SimPortfolio(cash=float(config["portfolio"]["initial_cash"]))
    previous_nav: float | None = None
    high_watermark = portfolio.cash
    invalid_days = 0
    turnovers: list[float] = []

    with get_connection(read_only=False) as conn:
        conn.execute(
            """
            INSERT INTO llm_backtest_runs (
                run_id, preset, start_date, end_date, status, model, mode, config_json
            )
            VALUES (?, ?, ?, ?, 'running', ?, ?, ?)
            ON CONFLICT (run_id) DO UPDATE SET status = 'running'
            """,
            [
                run_id,
                args.preset,
                dates[0],
                dates[-1],
                "mock" if args.mock_llm else None,
                mode,
                json.dumps(config, sort_keys=True, default=str),
            ],
        )

    try:
        for trade_date in dates[1:]:
            as_of_date = _previous_date(dates, trade_date)
            if as_of_date is None:
                continue
            prices = _price_map(trade_date)
            current_weights = _current_weights(portfolio, prices)
            qaoa_signal = run_qaoa_optimizer(
                as_of_date=as_of_date,
                current_weights=current_weights,
                config=qaoa_config,
                persist=True,
            )
            prompt_context = _build_prompt_context(
                as_of_date=as_of_date,
                trade_date=trade_date,
                portfolio=portfolio,
                prices=prices,
                qaoa_signal=qaoa_signal,
                config=config,
            )
            validation_errors: list[str] = []
            parsed: dict[str, Any] | None = None
            raw_response = ""
            latency: float | None = None
            attempts = 2 if bool(config.get("runtime", {}).get("retry_invalid_once", True)) else 1
            for attempt in range(1, attempts + 1):
                try:
                    if args.mock_llm:
                        parsed = _mock_llm_decision(prompt_context)
                        raw_response = json.dumps(parsed, ensure_ascii=False)
                        latency = 0.0
                    else:
                        raw_response, latency = _call_vllm(prompt_context, config)
                        parsed = _parse_json_response(raw_response)
                    decisions, validation_errors = _validate_decision(
                        parsed,
                        context=prompt_context,
                        portfolio=portfolio,
                        trade_date=trade_date,
                    )
                except Exception as exc:
                    parsed = None
                    decisions = []
                    validation_errors = [f"{type(exc).__name__}: {exc}"]
                _persist_prompt_event(
                    run_id=run_id,
                    as_of_date=as_of_date,
                    trade_date=trade_date,
                    attempt=attempt,
                    prompt_context=prompt_context,
                    raw_response=raw_response,
                    parsed=parsed,
                    validation_errors=validation_errors,
                    latency=latency,
                )
                if not validation_errors:
                    break
                prompt_context["previous_validation_errors"] = validation_errors
            if validation_errors:
                invalid_days += 1
                decisions = []
            qaoa_selected = set(qaoa_signal.get("selected_symbols") or [])
            _persist_decisions(
                run_id=run_id,
                trade_date=trade_date,
                as_of_date=as_of_date,
                decisions=decisions,
                qaoa_selected=qaoa_selected,
                validation_errors=validation_errors,
            )
            _, turnover = _execute_rebalance(
                run_id=run_id,
                portfolio=portfolio,
                decisions=decisions,
                qaoa_signal=qaoa_signal,
                trade_date=trade_date,
                prices=prices,
                config=config,
            )
            turnovers.append(turnover)
            llm_selected = {row["symbol"] for row in decisions if row["target_weight"] > 0}
            previous_nav, high_watermark = _mark_to_market(
                run_id=run_id,
                portfolio=portfolio,
                trade_date=trade_date,
                prices=prices,
                previous_nav=previous_nav,
                high_watermark=high_watermark,
                qaoa_selected=qaoa_selected,
                llm_selected=llm_selected,
            )
        metrics = _compute_metrics(run_id, float(config["portfolio"]["initial_cash"]), turnovers, invalid_days, len(dates) - 1)
        report_path = _write_report(run_id, metrics, config, start_date=dates[0], end_date=dates[-1])
        with get_connection(read_only=False) as conn:
            conn.execute(
                "UPDATE llm_backtest_runs SET status = 'success', report_path = ? WHERE run_id = ?",
                [str(report_path.relative_to(REPO_ROOT)), run_id],
            )
        return {"run_id": run_id, "status": "success", "report_path": str(report_path), "metrics": metrics}
    except Exception as exc:
        with get_connection(read_only=False) as conn:
            conn.execute(
                "UPDATE llm_backtest_runs SET status = 'failed', error_message = ? WHERE run_id = ?",
                [str(exc), run_id],
            )
        raise


def parse_args() -> argparse.Namespace:
    """Parse CLI arguments."""
    parser = argparse.ArgumentParser(description="Backtest BQuant VLLM decisions with TA and QAOA signals.")
    parser.add_argument("--preset", default="smoke", choices=["smoke", "core", "full"])
    parser.add_argument("--start-date")
    parser.add_argument("--end-date")
    parser.add_argument("--run-id")
    parser.add_argument("--mock-llm", action="store_true", help="Use deterministic QAOA-following mock responses.")
    return parser.parse_args()


def main() -> None:
    """CLI entrypoint."""
    result = run_backtest(parse_args())
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
