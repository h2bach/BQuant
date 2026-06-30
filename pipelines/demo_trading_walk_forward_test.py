"""Walk-forward validation for BQuant agent stock selection.

This script simulates hiding future data from the agent by scoring symbols
strictly as of the trading day before a hidden window, then evaluates the
selected/ranked symbols against the actual later OHLCV data. It does not write
orders, recommendations, or portfolio state to DuckDB.
"""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

from agents.config import load_agent_config
from agents.context_store import load_agent_context
from agents.deterministic_agents import AgentDecision, apply_portfolio_overlay, score_context_row
from pipelines.live_update_runtime import is_trading_day
from warehouse.duckdb_connection import get_connection


REPO_ROOT = Path(__file__).resolve().parent.parent
INITIAL_CAPITAL = 50_000_000.0
BUY_FEE_RATE = 0.0015
SELL_FEE_RATE = 0.0015
SELL_TAX_RATE = 0.001
PRICE_VND_MULTIPLIER = 1000.0
PRICE_ALREADY_VND_THRESHOLD = 1000.0


def _parse_date(value: str) -> date:
    """Parse a YYYY-MM-DD date string."""
    return datetime.strptime(value, "%Y-%m-%d").date()


def _previous_trading_day(anchor: date) -> date:
    """Return the previous configured trading day before an anchor date."""
    current = anchor - timedelta(days=1)
    while not is_trading_day(current):
        current -= timedelta(days=1)
    return current


def _to_vnd_price(value: Any) -> float:
    """Normalize HOSE quote values to VND per share."""
    price = float(value or 0)
    return price * PRICE_VND_MULTIPLIER if 0 < price < PRICE_ALREADY_VND_THRESHOLD else price


def _net_return(gross_return: float) -> float:
    """Approximate round-trip return after demo buy fee, sell fee, and sell tax."""
    return (1 + gross_return) * (1 - BUY_FEE_RATE) * (1 - SELL_FEE_RATE - SELL_TAX_RATE) - 1


def _load_forward_returns(start_date: date, end_date: date) -> pd.DataFrame:
    """Load entry/exit prices and forward returns for VN30 constituents."""
    with get_connection(read_only=True) as conn:
        return conn.execute(
            """
            WITH start_px AS (
                SELECT symbol, open AS entry_open, close AS start_close
                FROM daily_ohlcv_base
                WHERE trading_date = ?
            ),
            end_px AS (
                SELECT symbol, close AS exit_close
                FROM daily_ohlcv_base
                WHERE trading_date = ?
            )
            SELECT s.symbol,
                   s.entry_open,
                   s.start_close,
                   e.exit_close,
                   e.exit_close / nullif(s.entry_open, 0) - 1 AS gross_return_open_to_close,
                   e.exit_close / nullif(s.start_close, 0) - 1 AS gross_return_close_to_close
            FROM start_px s
            INNER JOIN end_px e USING(symbol)
            ORDER BY s.symbol
            """,
            [start_date, end_date],
        ).df()


def _load_index_returns(start_date: date, end_date: date) -> list[dict[str, Any]]:
    """Load VNINDEX/VN30 forward returns for the same window."""
    with get_connection(read_only=True) as conn:
        rows = conn.execute(
            """
            SELECT start.symbol,
                   start.open AS entry_open,
                   finish.close AS exit_close,
                   finish.close / nullif(start.open, 0) - 1 AS gross_return
            FROM market_index_daily_base start
            INNER JOIN market_index_daily_base finish
              ON start.symbol = finish.symbol
            WHERE start.trading_date = ?
              AND finish.trading_date = ?
            ORDER BY start.symbol
            """,
            [start_date, end_date],
        ).fetchall()
    return [
        {
            "symbol": row[0],
            "entry_open": float(row[1]),
            "exit_close": float(row[2]),
            "gross_return": float(row[3]),
        }
        for row in rows
    ]


def _score_as_of(as_of_date: date) -> list[AgentDecision]:
    """Run deterministic agents in memory for one as-of date."""
    config = load_agent_config()
    context = load_agent_context(as_of_date=as_of_date, config=config)
    decisions = [score_context_row(row, config) for _, row in context.iterrows()]
    return apply_portfolio_overlay(decisions, config)


def _equal_weight_summary(decisions: list[AgentDecision], returns: pd.DataFrame, top_n: int) -> dict[str, Any]:
    """Evaluate an equal-weight top-N basket."""
    return_by_symbol = {str(row["symbol"]): row for _, row in returns.iterrows()}
    selected = [decision for decision in decisions[:top_n] if decision.symbol in return_by_symbol]
    if not selected:
        return {"top_n": top_n, "gross_return": 0.0, "net_return": 0.0, "rows": []}
    rows: list[dict[str, Any]] = []
    for decision in selected:
        row = return_by_symbol[decision.symbol]
        gross_return = float(row["gross_return_open_to_close"])
        rows.append(
            {
                "symbol": decision.symbol,
                "recommendation": decision.recommendation,
                "score": decision.score,
                "confidence": decision.confidence,
                "risk_level": decision.risk_level,
                "entry_open": float(row["entry_open"]),
                "exit_close": float(row["exit_close"]),
                "gross_return": gross_return,
                "net_return": _net_return(gross_return),
            }
        )
    return {
        "top_n": top_n,
        "gross_return": sum(row["gross_return"] for row in rows) / len(rows),
        "net_return": sum(row["net_return"] for row in rows) / len(rows),
        "rows": rows,
    }


def _strict_summary(decisions: list[AgentDecision], returns: pd.DataFrame) -> dict[str, Any]:
    """Evaluate the strict app rule: buy candidate_long symbols only."""
    return_by_symbol = {str(row["symbol"]): row for _, row in returns.iterrows()}
    selected = [
        decision
        for decision in decisions
        if decision.recommendation == "candidate_long"
        and decision.suggested_weight > 0
        and decision.symbol in return_by_symbol
    ]
    if not selected:
        return {"positions": 0, "gross_return": 0.0, "net_return": 0.0, "cash_weight": 1.0, "rows": []}
    weight_total = sum(decision.suggested_weight for decision in selected)
    rows: list[dict[str, Any]] = []
    for decision in selected:
        row = return_by_symbol[decision.symbol]
        gross_return = float(row["gross_return_open_to_close"])
        rows.append(
            {
                "symbol": decision.symbol,
                "weight": decision.suggested_weight / weight_total,
                "gross_return": gross_return,
                "net_return": _net_return(gross_return),
            }
        )
    return {
        "positions": len(selected),
        "gross_return": sum(row["gross_return"] * row["weight"] for row in rows),
        "net_return": sum(row["net_return"] * row["weight"] for row in rows),
        "cash_weight": 0.0,
        "rows": rows,
    }


def build_report(hidden_start: date, end_date: date, top_n_values: list[int]) -> str:
    """Build a Markdown walk-forward report."""
    as_of_date = _previous_trading_day(hidden_start)
    decisions = sorted(_score_as_of(as_of_date), key=lambda item: (item.score, item.confidence), reverse=True)
    returns = _load_forward_returns(hidden_start, end_date)
    strict = _strict_summary(decisions, returns)
    forced = [_equal_weight_summary(decisions, returns, top_n) for top_n in top_n_values]
    index_returns = _load_index_returns(hidden_start, end_date)
    equal_weight_gross = float(returns["gross_return_open_to_close"].mean()) if not returns.empty else 0.0

    lines = [
        "# Demo Trading Walk-Forward Test",
        "",
        f"- Hidden window: `{hidden_start}` to `{end_date}`",
        f"- Agent as-of date: `{as_of_date}`",
        f"- Initial demo capital: `{INITIAL_CAPITAL:,.0f} VND`",
        "- Execution assumption: buy at hidden-window first-day open, evaluate at end-date close.",
        "- Strict app rule: buy only `candidate_long` symbols; otherwise hold cash.",
        "",
        "## Agent State At Cutoff",
        "",
        f"- Recommendation counts: `{dict(Counter(decision.recommendation for decision in decisions))}`",
        "",
        "| Rank | Symbol | Recommendation | Score | Confidence | Risk | Suggested Weight |",
        "|---:|---|---|---:|---:|---|---:|",
    ]
    for rank, decision in enumerate(decisions[:12], start=1):
        lines.append(
            f"| {rank} | {decision.symbol} | {decision.recommendation} | "
            f"{decision.score:.3f} | {decision.confidence:.3f} | {decision.risk_level} | "
            f"{decision.suggested_weight:.2%} |"
        )

    lines.extend(
        [
            "",
            "## Portfolio Result",
            "",
            (
                f"- Strict app portfolio: `{strict['positions']}` positions, "
                f"gross `{strict['gross_return']:.2%}`, net `{strict['net_return']:.2%}`, "
                f"cash `{strict['cash_weight']:.0%}`"
            ),
            f"- VN30 constituent equal-weight gross: `{equal_weight_gross:.2%}`; net-if-traded `{_net_return(equal_weight_gross):.2%}`",
        ]
    )
    for row in index_returns:
        lines.append(f"- {row['symbol']} index gross: `{row['gross_return']:.2%}`")

    for summary in forced:
        lines.extend(
            [
                "",
                f"## Forced Top {summary['top_n']} Basket",
                "",
                f"- Equal-weight gross: `{summary['gross_return']:.2%}`",
                f"- Equal-weight net after fees/tax: `{summary['net_return']:.2%}`",
                "",
                "| Symbol | State | Score | Entry Open | Exit Close | Gross | Net |",
                "|---|---|---:|---:|---:|---:|---:|",
            ]
        )
        for item in summary["rows"]:
            lines.append(
                f"| {item['symbol']} | {item['recommendation']} | {item['score']:.3f} | "
                f"{item['entry_open']:.2f} | {item['exit_close']:.2f} | "
                f"{item['gross_return']:.2%} | {item['net_return']:.2%} |"
            )

    strict_phrase = "held cash" if strict["positions"] == 0 else f"opened {strict['positions']} strict positions"
    lines.extend(
        [
            "",
            "## Interpretation",
            "",
            (
                "- Under the current strict decision threshold, the agent "
                f"{strict_phrase}."
            ),
            "- The ranked watchlist can be evaluated as a separate deployment layer when strict candidate selection is too narrow.",
            "- This is a short 7-trading-day window, so it is a smoke test of workflow behavior rather than statistical validation.",
        ]
    )
    return "\n".join(lines) + "\n"


def main() -> None:
    """CLI entrypoint."""
    parser = argparse.ArgumentParser(description="Run a non-destructive Demo Trading walk-forward test.")
    parser.add_argument("--hidden-start", default="2026-06-22", help="First hidden/evaluation date.")
    parser.add_argument("--end-date", default="2026-06-30", help="Evaluation end date.")
    parser.add_argument("--top-n", default="5,10", help="Comma-separated forced basket sizes.")
    parser.add_argument("--report-path", default="", help="Optional Markdown output path.")
    args = parser.parse_args()

    hidden_start = _parse_date(args.hidden_start)
    end_date = _parse_date(args.end_date)
    top_n_values = [int(value.strip()) for value in args.top_n.split(",") if value.strip()]
    report = build_report(hidden_start, end_date, top_n_values)
    print(report)
    if args.report_path:
        output_path = Path(args.report_path)
        if not output_path.is_absolute():
            output_path = REPO_ROOT / output_path
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(report, encoding="utf-8")


if __name__ == "__main__":
    main()
