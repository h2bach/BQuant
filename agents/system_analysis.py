"""System-level BQuant agents for data health, TA summaries, and portfolio review."""

from __future__ import annotations

import json
import math
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
import yaml

from agents.knowledge_base import render_knowledge_context
from agents.llm_client import chat_completion, check_llm_available, load_llm_config, runtime_config
from agents.orchestrator import run_agent_cycle
from pipelines.live_update_runtime import (
    create_run_id,
    eod_reconcile_time,
    is_trading_day,
    latest_eligible_intraday_slot,
    now_local,
    record_pipeline_run,
)
from utils.logger import BQuantLogger
from warehouse.duckdb_connection import get_connection
from warehouse.refresh_state import bump_refresh_version, ensure_refresh_state_table, get_refresh_state


REPO_ROOT = Path(__file__).resolve().parent.parent
LLM_RUNTIME_CONFIG_PATH = REPO_ROOT / "configs" / "llm_runtime.yaml"
PIPELINE_NAME = "run_agent_system_analysis"
DATASET_NAME = "agent_system_analysis"
AGENT_VERSION = "system-analysis-v1"


def _json_default(value: Any) -> str:
    """Serialize non-standard scalar values for JSON payloads.

    Args:
        value: Value produced by pandas, DuckDB, or Python date/time APIs.

    Returns:
        ISO/string representation safe for JSON storage.
    """
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    return str(value)


def _safe_float(value: Any, default: float = 0.0) -> float:
    """Convert an optional scalar to a finite float.

    Args:
        value: Raw scalar returned from pandas/DuckDB.
        default: Fallback value for null, NaN, or non-finite inputs.

    Returns:
        Finite float suitable for scoring and display.
    """
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


def _fmt_pct(value: Any, digits: int = 2) -> str:
    """Format a decimal return as a percentage string.

    Args:
        value: Decimal value such as `0.0123`.
        digits: Number of decimal places to display.

    Returns:
        Percentage string, or `N/A` for null/non-finite values.
    """
    if value is None or pd.isna(value):
        return "N/A"
    return f"{_safe_float(value) * 100:+.{digits}f}%"


def _fmt_number(value: Any, digits: int = 2) -> str:
    """Format a numeric scalar with thousands separators.

    Args:
        value: Raw scalar value.
        digits: Decimal places for float-like values.

    Returns:
        Human-readable number string.
    """
    if value is None or pd.isna(value):
        return "N/A"
    return f"{_safe_float(value):,.{digits}f}"


def _load_yaml(path: Path) -> dict[str, Any]:
    """Load a YAML configuration file if it exists.

    Args:
        path: Absolute path to the YAML file.

    Returns:
        Parsed mapping, or an empty dictionary for missing/empty files.
    """
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def ensure_system_analysis_tables() -> None:
    """Create system-analysis persistence objects when schema init has not run.

    Side Effects:
        Executes idempotent DDL in the primary DuckDB warehouse.
    """
    with get_connection(read_only=False) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS agent_system_analysis_runs (
                run_id VARCHAR PRIMARY KEY,
                analysis_date DATE NOT NULL,
                trigger_type VARCHAR NOT NULL,
                status VARCHAR NOT NULL,
                report_markdown VARCHAR NOT NULL,
                sections_json VARCHAR NOT NULL,
                question VARCHAR,
                answer_markdown VARCHAR,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_agent_system_analysis_runs_date
            ON agent_system_analysis_runs(analysis_date)
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_agent_system_analysis_runs_status
            ON agent_system_analysis_runs(status)
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS agent_chat_messages (
                chat_id VARCHAR PRIMARY KEY,
                event_ts TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                question VARCHAR NOT NULL,
                answer_markdown VARCHAR NOT NULL,
                answer_source VARCHAR NOT NULL,
                model VARCHAR,
                latency_seconds DOUBLE,
                sections_json VARCHAR NOT NULL,
                error_message VARCHAR
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_agent_chat_messages_event_ts
            ON agent_chat_messages(event_ts)
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_agent_chat_messages_source
            ON agent_chat_messages(answer_source)
            """
        )
        conn.execute(
            """
            CREATE OR REPLACE VIEW v_latest_agent_system_analysis AS
            SELECT *
            FROM agent_system_analysis_runs
            WHERE status = 'success'
            ORDER BY created_at DESC
            LIMIT 1
            """
        )
        conn.execute(
            """
            CREATE OR REPLACE VIEW v_latest_agent_chat_messages AS
            SELECT *
            FROM agent_chat_messages
            ORDER BY event_ts DESC
            LIMIT 100
            """
        )


def previous_trading_date(anchor: date, *, inclusive: bool = True) -> date:
    """Find the previous configured trading day.

    Args:
        anchor: Date to inspect.
        inclusive: Whether `anchor` itself can be returned.

    Returns:
        Previous non-weekend trading date according to `configs/live_update.yaml`.
    """
    current = anchor if inclusive else anchor - timedelta(days=1)
    while not is_trading_day(current):
        current -= timedelta(days=1)
    return current


def expected_daily_date(moment: datetime | None = None) -> date:
    """Resolve the daily bar date that BQuant should have available now.

    Args:
        moment: Optional timezone-aware market timestamp. Defaults to local
            market time.

    Returns:
        Current date after EOD buffer on trading days; otherwise the previous
        trading day. This intentionally handles weekend skips before the
        platform has a full exchange-holiday calendar.
    """
    current = moment or now_local()
    if is_trading_day(current.date()) and current >= eod_reconcile_time(current.date()):
        return current.date()
    return previous_trading_date(current.date(), inclusive=False)


def _trading_day_gap(latest: date | None, expected: date) -> int:
    """Count configured trading days between a latest date and expected date.

    Args:
        latest: Latest available date in a dataset.
        expected: Expected latest date for current market time.

    Returns:
        Non-negative number of missing trading days.
    """
    if latest is None:
        return 999
    if latest >= expected:
        return 0
    missing = 0
    cursor = latest + timedelta(days=1)
    while cursor <= expected:
        if is_trading_day(cursor):
            missing += 1
        cursor += timedelta(days=1)
    return missing


def _single_row(sql: str, params: list[Any] | None = None) -> dict[str, Any]:
    """Run a query expected to return one row.

    Args:
        sql: DuckDB SQL statement.
        params: Optional positional parameters.

    Returns:
        Dictionary for the first row, or an empty dictionary when no row exists.
    """
    with get_connection(read_only=True) as conn:
        frame = conn.execute(sql, params or []).df()
    if frame.empty:
        return {}
    return frame.iloc[0].to_dict()


def _frame(sql: str, params: list[Any] | None = None) -> pd.DataFrame:
    """Run a read-only query into a DataFrame.

    Args:
        sql: DuckDB SQL statement.
        params: Optional positional parameters.

    Returns:
        Query result as a pandas DataFrame.
    """
    with get_connection(read_only=True) as conn:
        return conn.execute(sql, params or []).df()


def collect_data_health() -> dict[str, Any]:
    """Run the data-health agent over warehouse, manifest, and refresh state.

    Returns:
        Structured data-health summary used by the report and web UI.
    """
    current = now_local()
    expected_date = expected_daily_date(current)
    eligible_slot = latest_eligible_intraday_slot(current)

    daily = _single_row(
        """
        WITH per_symbol AS (
            SELECT symbol,
                   min(trading_date) AS first_date,
                   max(trading_date) AS latest_date,
                   count(*) AS row_count
            FROM daily_ohlcv_base
            GROUP BY symbol
        )
        SELECT min(first_date) AS first_date,
               max(latest_date) AS latest_date,
               min(latest_date) AS coverage_floor_date,
               sum(row_count) AS row_count,
               count(*) AS symbol_count
        FROM per_symbol
        """
    )
    indices = _single_row(
        """
        WITH per_symbol AS (
            SELECT symbol,
                   min(trading_date) AS first_date,
                   max(trading_date) AS latest_date,
                   count(*) AS row_count
            FROM market_index_daily_base
            GROUP BY symbol
        )
        SELECT min(first_date) AS first_date,
               max(latest_date) AS latest_date,
               min(latest_date) AS coverage_floor_date,
               sum(row_count) AS row_count,
               count(*) AS symbol_count
        FROM per_symbol
        """
    )
    intraday = _single_row(
        """
        SELECT min(bar_time) AS first_bar_time,
               max(bar_time) AS latest_bar_time,
               count(*) AS row_count,
               count(DISTINCT symbol) AS symbol_count
        FROM v_intraday_15m_plot_universe
        """
    )
    manifest = _frame(
        """
        SELECT dataset_name, update_status, count(*) AS row_count
        FROM data_file_manifest
        GROUP BY dataset_name, update_status
        ORDER BY dataset_name, update_status
        """
    )
    quality = _frame(
        """
        SELECT data_quality_status, count(*) AS symbol_count
        FROM analytics_marts.mart_symbol_data_quality
        GROUP BY data_quality_status
        ORDER BY data_quality_status
        """
    )

    daily_latest = (
        pd.Timestamp(daily.get("coverage_floor_date")).date() if daily.get("coverage_floor_date") is not None else None
    )
    index_latest = (
        pd.Timestamp(indices.get("coverage_floor_date")).date()
        if indices.get("coverage_floor_date") is not None
        else None
    )
    latest_intraday = (
        pd.Timestamp(intraday.get("latest_bar_time")).to_pydatetime()
        if intraday.get("latest_bar_time") is not None
        else None
    )
    daily_gap = _trading_day_gap(daily_latest, expected_date)
    index_gap = _trading_day_gap(index_latest, expected_date)
    intraday_due = eligible_slot is not None
    intraday_stale = bool(intraday_due and (latest_intraday is None or latest_intraday < eligible_slot.replace(tzinfo=None)))
    quality_failures = 0
    if not quality.empty:
        quality_failures = int(quality.loc[quality["data_quality_status"] != "pass", "symbol_count"].sum())
    stale_manifest_rows = 0
    awaiting_refresh_rows = 0
    if not manifest.empty:
        stale_manifest_rows = int(
            manifest.loc[
                manifest["update_status"].isin(["stale", "pending_merge"]),
                "row_count",
            ].sum()
        )
        awaiting_refresh_rows = int(
            manifest.loc[manifest["update_status"].isin(["awaiting_refresh_window"]), "row_count"].sum()
        )

    status = "healthy"
    if daily_gap > 0 or index_gap > 0 or intraday_stale:
        status = "stale"
    if quality_failures > 0 or stale_manifest_rows > 0:
        status = "warning" if status == "healthy" else status

    return {
        "agent": "data_health_agent",
        "status": status,
        "current_time": current.isoformat(),
        "expected_daily_date": expected_date.isoformat(),
        "latest_eligible_intraday_slot": eligible_slot.isoformat() if eligible_slot else None,
        "daily": {
            **daily,
            "coverage_floor_date": daily_latest.isoformat() if daily_latest else None,
            "missing_trading_days": daily_gap,
            "refresh_state": get_refresh_state("daily_ohlcv_10y"),
        },
        "market_indices": {
            **indices,
            "coverage_floor_date": index_latest.isoformat() if index_latest else None,
            "missing_trading_days": index_gap,
            "refresh_state": get_refresh_state("market_index_daily_10y"),
        },
        "intraday": {
            **intraday,
            "latest_bar_time": latest_intraday.isoformat() if latest_intraday else None,
            "stale": intraday_stale,
            "refresh_state": get_refresh_state("intraday_ohlcv_15m_delta"),
        },
        "manifest_status": manifest.to_dict("records"),
        "quality_status": quality.to_dict("records"),
        "quality_failure_count": quality_failures,
        "stale_manifest_rows": stale_manifest_rows,
        "awaiting_refresh_rows": awaiting_refresh_rows,
    }


def _market_row_to_summary(row: pd.Series) -> dict[str, Any]:
    """Convert one market-index return row into a compact TA summary.

    Args:
        row: Row from `analytics_intermediate.int_market_index_returns`.

    Returns:
        Dictionary with trend, return, volatility, and moving-average fields.
    """
    close = _safe_float(row.get("close"))
    sma20 = _safe_float(row.get("sma_20"))
    sma50 = _safe_float(row.get("sma_50"))
    sma200 = _safe_float(row.get("sma_200"))
    trend_parts: list[str] = []
    if close > sma20:
        trend_parts.append("above SMA20")
    else:
        trend_parts.append("below SMA20")
    if close > sma50:
        trend_parts.append("above SMA50")
    else:
        trend_parts.append("below SMA50")
    if close > sma200:
        trend_parts.append("above SMA200")
    else:
        trend_parts.append("below SMA200")

    return {
        "symbol": str(row.get("symbol")),
        "trading_date": str(row.get("trading_date")),
        "close": close,
        "return_1d": _safe_float(row.get("return_1d")),
        "return_5d": _safe_float(row.get("return_5d")),
        "return_20d": _safe_float(row.get("return_20d")),
        "return_60d": _safe_float(row.get("return_60d")),
        "volatility_20d": _safe_float(row.get("volatility_20d")),
        "volatility_60d": _safe_float(row.get("volatility_60d")),
        "sma_20": sma20,
        "sma_50": sma50,
        "sma_200": sma200,
        "trend_summary": ", ".join(trend_parts),
    }


def collect_chart_ta_summary() -> dict[str, Any]:
    """Run the chart/TA agent over market regime and index-return marts.

    Returns:
        Structured day/week/month market summary for VNIndex and VN30.
    """
    regime = _single_row(
        """
        SELECT *
        FROM analytics_marts.mart_market_regime_daily
        ORDER BY trading_date DESC
        LIMIT 1
        """
    )
    index_rows = _frame(
        """
        WITH ranked AS (
            SELECT *,
                   row_number() OVER (PARTITION BY symbol ORDER BY trading_date DESC) AS rn
            FROM analytics_intermediate.int_market_index_returns
            WHERE symbol IN ('VNINDEX', 'VN30')
        )
        SELECT *
        FROM ranked
        WHERE rn = 1
        ORDER BY symbol
        """
    )
    summaries = {
        str(row["symbol"]): _market_row_to_summary(row)
        for _, row in index_rows.iterrows()
    }
    breadth = {
        "active_symbols": int(regime.get("active_symbols") or 0),
        "advancers": int(regime.get("advancers") or 0),
        "decliners": int(regime.get("decliners") or 0),
        "advancer_ratio": _safe_float(regime.get("advancer_ratio")),
        "breadth_score": _safe_float(regime.get("breadth_score")),
    }
    return {
        "agent": "chart_ta_agent",
        "trading_date": str(regime.get("trading_date")) if regime else None,
        "market_regime": regime.get("market_regime") if regime else None,
        "volatility_regime": regime.get("volatility_regime") if regime else None,
        "regime_score": _safe_float(regime.get("regime_score")) if regime else None,
        "breadth": breadth,
        "indices": summaries,
    }


def collect_portfolio_summary(*, refresh_recommendations: bool, trigger_type: str) -> dict[str, Any]:
    """Run/read portfolio optimizer agent outputs.

    Args:
        refresh_recommendations: Whether to run the deterministic agent cycle
            before summarizing recommendations.
        trigger_type: Trigger metadata propagated to an optional recommendation
            cycle.

    Returns:
        Portfolio summary with recommendation counts and top-ranked symbols.
    """
    cycle_result: dict[str, Any] | None = None
    if refresh_recommendations:
        cycle_result = run_agent_cycle(trigger_type=trigger_type)

    recommendations = _frame(
        """
        SELECT *
        FROM v_latest_agent_recommendations
        ORDER BY suggested_weight DESC, score DESC, confidence DESC, symbol
        """
    )
    if recommendations.empty:
        return {
            "agent": "portfolio_optimizer_agent",
            "status": "no_recommendations",
            "cycle_result": cycle_result,
            "recommendation_counts": [],
            "top_weights": [],
            "watchlist": [],
            "risk_off": [],
        }

    counts = (
        recommendations.groupby("recommendation", dropna=False)
        .size()
        .reset_index(name="count")
        .sort_values(["recommendation"])
        .to_dict("records")
    )
    top_weights = recommendations.loc[recommendations["suggested_weight"] > 0].head(10).to_dict("records")
    watchlist = (
        recommendations.loc[recommendations["recommendation"].isin(["candidate_long", "watch"])]
        .sort_values(["score", "confidence"], ascending=False)
        .head(10)
        .to_dict("records")
    )
    risk_off = (
        recommendations.loc[recommendations["recommendation"].isin(["avoid_or_reduce", "blocked_data_quality"])]
        .sort_values(["score", "confidence"], ascending=True)
        .head(10)
        .to_dict("records")
    )
    as_of_date = str(recommendations["as_of_date"].max())
    return {
        "agent": "portfolio_optimizer_agent",
        "status": "ready",
        "as_of_date": as_of_date,
        "cycle_result": cycle_result,
        "recommendation_counts": counts,
        "top_weights": top_weights,
        "watchlist": watchlist,
        "risk_off": risk_off,
    }


def load_llm_runtime_plan() -> dict[str, Any]:
    """Load the local LLM runtime recommendation/configuration.

    Returns:
        `configs/llm_runtime.yaml` mapping plus a concise runtime status.
    """
    config = load_llm_config(LLM_RUNTIME_CONFIG_PATH)
    runtime = runtime_config(config)
    gpu_profile = config.get("gpu_profile", {})
    candidates = config.get("candidate_models", {})
    return {
        "agent": "llm_runtime_planner",
        "enabled": bool(runtime.get("enabled", False)),
        "backend": runtime.get("backend", "disabled"),
        "default_model": runtime.get("default_model"),
        "endpoint": runtime.get("endpoint"),
        "timeout_seconds": runtime.get("timeout_seconds"),
        "fallback_to_deterministic": runtime.get("fallback_to_deterministic"),
        "recommended_concurrent_llms": gpu_profile.get("recommended_concurrent_llms", 1),
        "gpu_profile": gpu_profile,
        "candidate_models": candidates,
        "runtime": runtime,
        "availability": check_llm_available(config),
    }


def _render_report(sections: dict[str, Any]) -> str:
    """Render structured agent sections into Markdown.

    Args:
        sections: Output from data, TA, portfolio, and runtime planner agents.

    Returns:
        Markdown report stored in DuckDB and shown in the web UI.
    """
    health = sections["data_health"]
    market = sections["chart_ta"]
    portfolio = sections["portfolio"]
    llm = sections["llm_runtime"]
    lines: list[str] = [
        "# BQuant Agentic System Analysis",
        "",
        f"- Run date: {sections['analysis_date']}",
        f"- Agent version: {AGENT_VERSION}",
        "",
        "## Data Health Agent",
        "",
        f"- Overall status: **{health['status']}**",
        f"- Expected daily date: `{health['expected_daily_date']}`",
        (
            "- Daily VN30 base: "
            f"coverage floor `{health['daily'].get('coverage_floor_date')}`, "
            f"latest max `{health['daily'].get('latest_date')}`, "
            f"{int(health['daily'].get('symbol_count') or 0)} symbols, "
            f"{int(health['daily'].get('row_count') or 0):,} rows, "
            f"missing {health['daily'].get('missing_trading_days')} trading days"
        ),
        (
            "- Market index base: "
            f"coverage floor `{health['market_indices'].get('coverage_floor_date')}`, "
            f"latest max `{health['market_indices'].get('latest_date')}`, "
            f"{int(health['market_indices'].get('symbol_count') or 0)} indices, "
            f"{int(health['market_indices'].get('row_count') or 0):,} rows, "
            f"missing {health['market_indices'].get('missing_trading_days')} trading days"
        ),
        (
            "- Intraday 15m: "
            f"latest `{health['intraday'].get('latest_bar_time')}`, "
            f"stale=`{health['intraday'].get('stale')}`"
        ),
        f"- Data quality failures: {health['quality_failure_count']}",
        f"- Stale/pending manifest rows: {health['stale_manifest_rows']}",
        f"- Awaiting refresh-window manifest rows: {health['awaiting_refresh_rows']}",
        "",
        "## Chart / TA Agent",
        "",
        (
            "- Market regime: "
            f"**{market.get('market_regime')}**, volatility regime "
            f"**{market.get('volatility_regime')}**, regime score "
            f"{_fmt_number(market.get('regime_score'), 3)}"
        ),
        (
            "- Breadth: "
            f"{market['breadth']['advancers']}/{market['breadth']['active_symbols']} advancing, "
            f"advancer ratio {_fmt_pct(market['breadth']['advancer_ratio'])}"
        ),
    ]
    for symbol in ["VNINDEX", "VN30"]:
        row = market.get("indices", {}).get(symbol)
        if not row:
            continue
        lines.extend(
            [
                (
                    f"- {symbol}: close {_fmt_number(row['close'])}; "
                    f"day {_fmt_pct(row['return_1d'])}, "
                    f"week {_fmt_pct(row['return_5d'])}, "
                    f"month {_fmt_pct(row['return_20d'])}, "
                    f"quarter {_fmt_pct(row['return_60d'])}; "
                    f"{row['trend_summary']}"
                )
            ]
        )

    lines.extend(
        [
            "",
            "## Portfolio Optimizer Agent",
            "",
            f"- Recommendation date: `{portfolio.get('as_of_date', 'N/A')}`",
            f"- Status: **{portfolio.get('status')}**",
        ]
    )
    counts = portfolio.get("recommendation_counts") or []
    if counts:
        count_text = ", ".join(f"{row['recommendation']}={row['count']}" for row in counts)
        lines.append(f"- Recommendation mix: {count_text}")
    watchlist = portfolio.get("watchlist") or []
    if watchlist:
        watch_text = ", ".join(
            f"{row['symbol']}({row['recommendation']}, score={_fmt_number(row['score'], 3)})"
            for row in watchlist[:5]
        )
        lines.append(f"- Highest ranked watchlist: {watch_text}")
    top_weights = portfolio.get("top_weights") or []
    if top_weights:
        weight_text = ", ".join(
            f"{row['symbol']}={_fmt_pct(row['suggested_weight'])}"
            for row in top_weights[:5]
        )
        lines.append(f"- Suggested allocation: {weight_text}")
    else:
        lines.append("- Suggested allocation: no long allocation under current thresholds.")

    lines.extend(
        [
            "",
            "## LLM Runtime Planner",
            "",
            f"- Runtime enabled: `{llm.get('enabled')}`",
            f"- Recommended concurrent resident LLMs: `{llm.get('recommended_concurrent_llms')}`",
            f"- Default model candidate: `{llm.get('default_model')}`",
            "- Current architecture: keep deterministic agents as tools; use one quantized 7B/8B LLM for chat/orchestration when enabled.",
        ]
    )
    return "\n".join(lines)


def _answer_from_sections(question: str, sections: dict[str, Any]) -> str:
    """Answer a chat question using the latest deterministic analysis.

    Args:
        question: User question from the web UI/CLI.
        sections: Structured report sections.

    Returns:
        Markdown answer grounded in the latest BQuant agent output.
    """
    lowered = question.lower()
    health = sections["data_health"]
    market = sections["chart_ta"]
    portfolio = sections["portfolio"]
    if any(keyword in lowered for keyword in ["data", "fresh", "update", "du lieu", "stale"]):
        return (
            "Data status: "
            f"{health['status']}. Daily coverage floor is {health['daily'].get('coverage_floor_date')} "
            f"while expected is {health['expected_daily_date']}. "
            f"Market index coverage floor is {health['market_indices'].get('coverage_floor_date')}. "
            f"Intraday latest is {health['intraday'].get('latest_bar_time')}."
        )
    if any(keyword in lowered for keyword in ["portfolio", "weight", "allocation", "danh muc", "toi uu"]):
        watchlist = portfolio.get("watchlist") or []
        names = ", ".join(row["symbol"] for row in watchlist[:5]) if watchlist else "no active candidates"
        return (
            "Portfolio view: "
            f"{portfolio.get('status')}. Current highest ranked names are {names}. "
            "Suggested weights remain conservative because allocation is only assigned to symbols crossing the candidate_long threshold."
        )
    if any(keyword in lowered for keyword in ["chart", "ta", "vnindex", "vn30", "market", "trend"]):
        return (
            "Market view: "
            f"{market.get('market_regime')} regime with {market.get('volatility_regime')}. "
            f"VNINDEX day/week/month returns are "
            f"{_fmt_pct(market.get('indices', {}).get('VNINDEX', {}).get('return_1d'))}, "
            f"{_fmt_pct(market.get('indices', {}).get('VNINDEX', {}).get('return_5d'))}, "
            f"{_fmt_pct(market.get('indices', {}).get('VNINDEX', {}).get('return_20d'))}. "
            f"VN30 day/week/month returns are "
            f"{_fmt_pct(market.get('indices', {}).get('VN30', {}).get('return_1d'))}, "
            f"{_fmt_pct(market.get('indices', {}).get('VN30', {}).get('return_5d'))}, "
            f"{_fmt_pct(market.get('indices', {}).get('VN30', {}).get('return_20d'))}."
        )
    return (
        "Latest BQuant analysis combines data health, market TA, and portfolio recommendations. "
        f"Data is {health['status']}; market regime is {market.get('market_regime')}; "
        f"portfolio agent status is {portfolio.get('status')}."
    )


def build_live_analysis_sections(
    *,
    refresh_recommendations: bool = False,
    trigger_type: str = "manual",
) -> dict[str, Any]:
    """Collect fresh deterministic context for a live LLM answer.

    Args:
        refresh_recommendations: Whether to run the recommendation agent cycle
            before creating the context.
        trigger_type: Manual/scheduled/recovery metadata used when refreshing
            recommendations.

    Returns:
        Structured context sections for data health, market TA, portfolio state,
        and local LLM runtime status.
    """
    return {
        "analysis_date": now_local().date().isoformat(),
        "agent_version": AGENT_VERSION,
        "data_health": collect_data_health(),
        "chart_ta": collect_chart_ta_summary(),
        "portfolio": collect_portfolio_summary(
            refresh_recommendations=refresh_recommendations,
            trigger_type=trigger_type,
        ),
        "llm_runtime": load_llm_runtime_plan(),
    }


def _compact_sections_for_llm(sections: dict[str, Any]) -> dict[str, Any]:
    """Reduce full agent sections to the fields needed by the chat model.

    Args:
        sections: Fresh output from `build_live_analysis_sections`.

    Returns:
        Compact JSON-safe context that fits a small local LLM prompt.
    """
    health = sections["data_health"]
    market = sections["chart_ta"]
    portfolio = sections["portfolio"]
    def _compact_rows(rows: list[dict[str, Any]] | None, limit: int = 6) -> list[dict[str, Any]]:
        """Keep only LLM-relevant fields from recommendation rows."""
        selected: list[dict[str, Any]] = []
        for row in (rows or [])[:limit]:
            selected.append(
                {
                    "symbol": row.get("symbol"),
                    "recommendation": row.get("recommendation"),
                    "confidence": row.get("confidence"),
                    "score": row.get("score"),
                    "risk_level": row.get("risk_level"),
                    "suggested_weight": row.get("suggested_weight"),
                }
            )
        return selected

    return {
        "analysis_date": sections.get("analysis_date"),
        "agent_version": sections.get("agent_version"),
        "data_health": {
            "status": health.get("status"),
            "expected_daily_date": health.get("expected_daily_date"),
            "daily_coverage_floor": health.get("daily", {}).get("coverage_floor_date"),
            "daily_missing_trading_days": health.get("daily", {}).get("missing_trading_days"),
            "market_index_coverage_floor": health.get("market_indices", {}).get("coverage_floor_date"),
            "market_index_missing_trading_days": health.get("market_indices", {}).get("missing_trading_days"),
            "intraday_latest_bar_time": health.get("intraday", {}).get("latest_bar_time"),
            "intraday_stale": health.get("intraday", {}).get("stale"),
            "quality_failure_count": health.get("quality_failure_count"),
            "stale_manifest_rows": health.get("stale_manifest_rows"),
            "awaiting_refresh_rows": health.get("awaiting_refresh_rows"),
        },
        "market_ta": {
            "trading_date": market.get("trading_date"),
            "market_regime": market.get("market_regime"),
            "volatility_regime": market.get("volatility_regime"),
            "regime_score": market.get("regime_score"),
            "breadth": market.get("breadth"),
            "indices": market.get("indices"),
        },
        "portfolio": {
            "status": portfolio.get("status"),
            "as_of_date": portfolio.get("as_of_date"),
            "recommendation_counts": portfolio.get("recommendation_counts"),
            "top_weights": _compact_rows(portfolio.get("top_weights"), limit=6),
            "watchlist": _compact_rows(portfolio.get("watchlist"), limit=6),
            "risk_off": _compact_rows(portfolio.get("risk_off"), limit=6),
        },
        "symbol_signals": _compact_symbol_signals(portfolio, limit=10),
        "llm_runtime": {
            "enabled": sections.get("llm_runtime", {}).get("enabled"),
            "backend": sections.get("llm_runtime", {}).get("backend"),
            "default_model": sections.get("llm_runtime", {}).get("default_model"),
            "availability": sections.get("llm_runtime", {}).get("availability"),
        },
    }


def _safe_json_loads(value: Any) -> dict[str, Any]:
    """Parse a JSON object value with a dictionary fallback.

    Args:
        value: Raw JSON string from DuckDB, or an already-decoded object.

    Returns:
        Parsed dictionary. Invalid or non-object payloads return `{}`.
    """
    if isinstance(value, dict):
        return value
    if not value:
        return {}
    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _round_optional(value: Any, digits: int = 6) -> float | None:
    """Return a rounded finite float or `None`.

    Args:
        value: Nullable numeric value.
        digits: Number of decimal places to keep.

    Returns:
        Rounded float, or `None` for null/non-finite inputs.
    """
    if value is None or pd.isna(value):
        return None
    number = _safe_float(value)
    return round(number, digits) if math.isfinite(number) else None


def _portfolio_signal_symbols(portfolio: dict[str, Any], limit: int) -> list[str]:
    """Select symbols that should receive detailed LLM signal context.

    Args:
        portfolio: Portfolio section from `collect_portfolio_summary`.
        limit: Maximum symbol count.

    Returns:
        Ordered unique symbols from top weights, watchlist, and risk-off rows.
    """
    symbols: list[str] = []
    for bucket in ["top_weights", "watchlist", "risk_off"]:
        for row in portfolio.get(bucket) or []:
            symbol = str(row.get("symbol") or "").upper().strip()
            if symbol and symbol not in symbols:
                symbols.append(symbol)
            if len(symbols) >= limit:
                return symbols
    return symbols[:limit]


def _latest_recommendation_rows_for_signals(
    *,
    symbols: list[str],
    as_of_date: date | None,
    limit: int,
) -> pd.DataFrame:
    """Load latest recommendation rows for compact symbol context.

    Args:
        symbols: Optional preferred symbols. If empty, the latest run's top
            rows are selected by suggested weight, score, and confidence.
        as_of_date: Optional recommendation date ceiling.
        limit: Maximum rows to return.

    Returns:
        Recommendation frame containing run/date/action/rationale fields.
    """
    date_clause = "AND runs.as_of_date <= ?" if as_of_date else ""
    params: list[Any] = [as_of_date] if as_of_date else []
    symbol_clause = ""
    if symbols:
        placeholders = ", ".join(["?"] * len(symbols))
        symbol_clause = f"AND recommendations.symbol IN ({placeholders})"
        params.extend(symbols)
    params.append(int(limit))
    return _frame(
        f"""
        WITH latest_run AS (
            SELECT run_id, as_of_date, created_at
            FROM agent_recommendation_runs runs
            WHERE status = 'success'
              {date_clause}
            ORDER BY as_of_date DESC, created_at DESC
            LIMIT 1
        )
        SELECT recommendations.run_id,
               recommendations.as_of_date,
               recommendations.symbol,
               recommendations.recommendation,
               recommendations.confidence,
               recommendations.score,
               recommendations.risk_level,
               recommendations.suggested_weight,
               recommendations.rationale_json
        FROM agent_recommendations recommendations
        INNER JOIN latest_run
          ON recommendations.run_id = latest_run.run_id
        WHERE 1=1
          {symbol_clause}
        ORDER BY recommendations.suggested_weight DESC,
                 recommendations.score DESC,
                 recommendations.confidence DESC,
                 recommendations.symbol
        LIMIT ?
        """,
        params,
    )


def _latest_context_rows_for_signals(symbols: list[str], as_of_date: date | None) -> pd.DataFrame:
    """Load latest mart context rows for selected symbols.

    Args:
        symbols: Symbols to fetch.
        as_of_date: Optional context date ceiling, usually recommendation
            `as_of_date`.

    Returns:
        Latest available `mart_agent_context_daily` row per symbol.
    """
    if not symbols:
        return pd.DataFrame()
    placeholders = ", ".join(["?"] * len(symbols))
    date_clause = "AND trading_date <= ?" if as_of_date else ""
    params: list[Any] = list(symbols)
    if as_of_date:
        params.append(as_of_date)
    return _frame(
        f"""
        WITH ranked AS (
            SELECT *,
                   row_number() OVER (PARTITION BY symbol ORDER BY trading_date DESC) AS rn
            FROM analytics_marts.mart_agent_context_daily
            WHERE symbol IN ({placeholders})
              {date_clause}
        )
        SELECT *
        FROM ranked
        WHERE rn = 1
        """,
        params,
    )


def _compact_symbol_signals(portfolio: dict[str, Any], limit: int = 10) -> list[dict[str, Any]]:
    """Build detailed symbol-level signal context for the live LLM prompt.

    Args:
        portfolio: Portfolio summary from `collect_portfolio_summary`.
        limit: Maximum number of symbols included in the compact prompt.

    Returns:
        List of compact signal dictionaries combining latest agent
        recommendations, deterministic rationale, and dbt mart features.
    """
    try:
        as_of_raw = portfolio.get("as_of_date")
        as_of_date = pd.Timestamp(as_of_raw).date() if as_of_raw else None
    except Exception:
        as_of_date = None

    preferred_symbols = _portfolio_signal_symbols(portfolio, limit)
    recommendations = _latest_recommendation_rows_for_signals(
        symbols=preferred_symbols,
        as_of_date=as_of_date,
        limit=limit,
    )
    if recommendations.empty:
        return []

    symbols = [str(symbol).upper() for symbol in recommendations["symbol"].tolist()]
    context = _latest_context_rows_for_signals(symbols, as_of_date)
    context_by_symbol = {
        str(row["symbol"]).upper(): row
        for _, row in context.iterrows()
    }

    compact: list[dict[str, Any]] = []
    for _, rec in recommendations.iterrows():
        symbol = str(rec.get("symbol") or "").upper()
        ctx = context_by_symbol.get(symbol)
        rationale = _safe_json_loads(rec.get("rationale_json"))
        symbol_rationale = rationale.get("symbol", {}) if isinstance(rationale.get("symbol"), dict) else {}
        market_rationale = rationale.get("market", {}) if isinstance(rationale.get("market"), dict) else {}
        risk_rationale = rationale.get("risk", {}) if isinstance(rationale.get("risk"), dict) else {}
        quality_rationale = rationale.get("quality", {}) if isinstance(rationale.get("quality"), dict) else {}
        component_scores = symbol_rationale.get("component_scores", {})
        compact.append(
            {
                "symbol": symbol,
                "recommendation": rec.get("recommendation"),
                "score": _round_optional(rec.get("score"), 4),
                "confidence": _round_optional(rec.get("confidence"), 4),
                "risk_level": rec.get("risk_level"),
                "suggested_weight": _round_optional(rec.get("suggested_weight"), 6),
                "source": {
                    "recommendation_as_of_date": str(rec.get("as_of_date")) if rec.get("as_of_date") is not None else None,
                    "context_date": str(ctx.get("trading_date")) if ctx is not None and ctx.get("trading_date") is not None else None,
                },
                "signals": {
                    "close": _round_optional(ctx.get("close")) if ctx is not None else None,
                    "volume": _round_optional(ctx.get("volume"), 0) if ctx is not None else None,
                    "return_1d": _round_optional(ctx.get("return_1d"), 6) if ctx is not None else None,
                    "return_5d": _round_optional(ctx.get("return_5d"), 6) if ctx is not None else None,
                    "return_20d": _round_optional(ctx.get("return_20d"), 6) if ctx is not None else None,
                    "return_60d": _round_optional(ctx.get("return_60d"), 6) if ctx is not None else None,
                    "volatility_20d": _round_optional(ctx.get("volatility_20d"), 6) if ctx is not None else None,
                    "volatility_60d": _round_optional(ctx.get("volatility_60d"), 6) if ctx is not None else None,
                    "drawdown_from_peak": _round_optional(ctx.get("drawdown_from_peak"), 6) if ctx is not None else None,
                    "relative_strength_20d": _round_optional(ctx.get("relative_strength_20d"), 6)
                    if ctx is not None
                    else None,
                    "relative_strength_60d": _round_optional(ctx.get("relative_strength_60d"), 6)
                    if ctx is not None
                    else None,
                    "traded_value_percentile": _round_optional(ctx.get("traded_value_cross_section_percentile"), 2)
                    if ctx is not None
                    else None,
                    "trend_state": ctx.get("trend_state") if ctx is not None else symbol_rationale.get("trend_state"),
                    "agent_candidate_state": ctx.get("agent_candidate_state") if ctx is not None else None,
                    "liquidity_state": ctx.get("liquidity_state") if ctx is not None else None,
                    "data_quality_status": ctx.get("data_quality_status")
                    if ctx is not None
                    else quality_rationale.get("data_quality_status"),
                    "stale_days": int(ctx.get("stale_days"))
                    if ctx is not None and ctx.get("stale_days") is not None and not pd.isna(ctx.get("stale_days"))
                    else quality_rationale.get("stale_days"),
                },
                "market_context": {
                    "market_regime": ctx.get("market_regime") if ctx is not None else market_rationale.get("market_regime"),
                    "volatility_regime": ctx.get("volatility_regime") if ctx is not None else market_rationale.get("volatility_regime"),
                    "regime_score": _round_optional(ctx.get("regime_score"), 4)
                    if ctx is not None
                    else _round_optional(market_rationale.get("regime_score"), 4),
                },
                "component_scores": {
                    "trend": _round_optional(component_scores.get("trend"), 4),
                    "relative_strength": _round_optional(component_scores.get("relative_strength"), 4),
                    "momentum": _round_optional(component_scores.get("momentum"), 4),
                    "volatility": _round_optional(component_scores.get("volatility"), 4),
                    "liquidity": _round_optional(component_scores.get("liquidity"), 4),
                },
                "rationale_summary": {
                    "quality_blocked": quality_rationale.get("blocked"),
                    "quality_reasons": quality_rationale.get("reasons"),
                    "market": _round_optional(market_rationale.get("contribution"), 4),
                    "symbol": _round_optional(symbol_rationale.get("contribution"), 4),
                    "risk_drawdown": _round_optional(risk_rationale.get("drawdown_from_peak"), 6),
                },
            }
        )
    return compact


def _build_llm_messages(
    question: str,
    sections: dict[str, Any],
    *,
    knowledge_context: str,
) -> list[dict[str, str]]:
    """Build a grounded chat prompt from the live BQuant context.

    Args:
        question: User question exactly as entered in the UI/CLI.
        sections: Fresh BQuant context sections.
        knowledge_context: Retrieved BQuant project/system knowledge.

    Returns:
        OpenAI-compatible message list.
    """
    context_json = json.dumps(
        _compact_sections_for_llm(sections),
        ensure_ascii=False,
        default=_json_default,
        sort_keys=True,
        separators=(",", ":"),
    )
    system_prompt = (
        "You are BQuant's live investment-analysis assistant. "
        "Answer in Vietnamese when the user writes Vietnamese; otherwise use the user's language. "
        "Ground every claim in the provided BQuant context. "
        "Be clear about data freshness, market signals, portfolio implications, and uncertainty. "
        "Do not claim guaranteed returns and do not invent data outside the context. "
        "Keep the answer concise but useful for decision support."
    )
    user_prompt = (
        "/no_think\n"
        "User question:\n"
        f"{question.strip()}\n\n"
        "Fresh BQuant context JSON:\n"
        f"```json\n{context_json}\n```\n\n"
        "Retrieved BQuant project knowledge:\n"
        f"{knowledge_context}\n\n"
        "Return a practical answer with these sections when relevant: "
        "Data status, Market/TA read, Portfolio view, Next action."
    )
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]


def record_agent_chat_message(
    *,
    chat_id: str,
    question: str,
    answer_markdown: str,
    answer_source: str,
    sections: dict[str, Any],
    model: str | None = None,
    latency_seconds: float | None = None,
    error_message: str | None = None,
) -> None:
    """Persist one live chat exchange for audit and debugging.

    Args:
        chat_id: Unique chat identifier.
        question: User question.
        answer_markdown: Rendered answer shown to the user.
        answer_source: `llm_live`, `deterministic_fallback`, or `error`.
        sections: Context sections used to answer the question.
        model: LLM model name when a model generated the answer.
        latency_seconds: Optional LLM request duration.
        error_message: Optional runtime failure that caused fallback.

    Side Effects:
        Upserts the chat row into DuckDB.
    """
    ensure_system_analysis_tables()
    with get_connection(read_only=False) as conn:
        conn.execute(
            """
            INSERT INTO agent_chat_messages (
                chat_id,
                question,
                answer_markdown,
                answer_source,
                model,
                latency_seconds,
                sections_json,
                error_message
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (chat_id) DO UPDATE SET
                question = excluded.question,
                answer_markdown = excluded.answer_markdown,
                answer_source = excluded.answer_source,
                model = excluded.model,
                latency_seconds = excluded.latency_seconds,
                sections_json = excluded.sections_json,
                error_message = excluded.error_message
            """,
            [
                chat_id,
                question,
                answer_markdown,
                answer_source,
                model,
                latency_seconds,
                json.dumps(sections, ensure_ascii=True, default=_json_default, sort_keys=True),
                error_message,
            ],
        )


def load_recent_agent_chat_messages(limit: int = 20) -> list[dict[str, Any]]:
    """Load recent live chat exchanges for the Agent page.

    Args:
        limit: Maximum number of question/answer pairs to return.

    Returns:
        Chronologically ordered chat rows. Each row contains the persisted
        question, answer, source, model, latency, and event timestamp.
    """
    ensure_system_analysis_tables()
    bounded_limit = max(1, min(int(limit), 100))
    with get_connection(read_only=True) as conn:
        frame = conn.execute(
            """
            SELECT chat_id,
                   event_ts,
                   question,
                   answer_markdown,
                   answer_source,
                   model,
                   latency_seconds,
                   error_message
            FROM agent_chat_messages
            ORDER BY event_ts DESC
            LIMIT ?
            """,
            [bounded_limit],
        ).df()
    if frame.empty:
        return []
    return frame.sort_values("event_ts").to_dict("records")


def answer_live_system_question(question: str) -> dict[str, Any]:
    """Answer a question with fresh BQuant context and a local live LLM.

    Args:
        question: User-entered question from the web UI or CLI.

    Returns:
        Result dictionary containing answer markdown, source label, model,
        latency, context sections, and optional fallback/error metadata.
    """
    if not question.strip():
        raise ValueError("Question is empty")

    chat_id = create_run_id()
    sections = build_live_analysis_sections(refresh_recommendations=False, trigger_type="manual")
    config = load_llm_config(LLM_RUNTIME_CONFIG_PATH)
    runtime = runtime_config(config)
    fallback_answer = _answer_from_sections(question, sections)
    answer_source = "deterministic_fallback"
    model = str(runtime.get("default_model") or "")
    latency_seconds: float | None = None
    error_message: str | None = None
    answer_markdown = fallback_answer

    availability = sections.get("llm_runtime", {}).get("availability") or {}
    if availability.get("available"):
        try:
            knowledge_context = render_knowledge_context(question, limit=2, max_total_chars=2000)
            llm_response = chat_completion(
                _build_llm_messages(question, sections, knowledge_context=knowledge_context),
                config=config,
            )
            answer_markdown = llm_response.text
            answer_source = "llm_live"
            model = llm_response.model
            latency_seconds = llm_response.latency_seconds
        except Exception as exc:
            error_message = str(exc)
            logger = BQuantLogger(
                "agent_live_chat",
                component="agent",
                subcomponent="live_chat",
                default_channel="pipeline",
            )
            logger.log_error(
                "answer_live_system_question",
                type(exc).__name__,
                str(exc),
                context={"chat_id": chat_id, "model": model, "fallback_enabled": runtime.get("fallback_to_deterministic")},
                channel="pipeline",
            )
            if not bool(runtime.get("fallback_to_deterministic", True)):
                answer_source = "error"
                answer_markdown = f"LLM runtime failed and fallback is disabled: {exc}"
                record_agent_chat_message(
                    chat_id=chat_id,
                    question=question,
                    answer_markdown=answer_markdown,
                    answer_source=answer_source,
                    sections=sections,
                    model=model,
                    latency_seconds=latency_seconds,
                    error_message=error_message,
                )
                raise
    else:
        error_message = str(availability.get("error") or "LLM runtime unavailable")

    record_agent_chat_message(
        chat_id=chat_id,
        question=question,
        answer_markdown=answer_markdown,
        answer_source=answer_source,
        sections=sections,
        model=model or None,
        latency_seconds=latency_seconds,
        error_message=error_message,
    )
    return {
        "chat_id": chat_id,
        "answer_markdown": answer_markdown,
        "answer_source": answer_source,
        "model": model or None,
        "latency_seconds": latency_seconds,
        "sections": sections,
        "error_message": error_message,
        "availability": availability,
    }


def record_system_analysis_run(
    *,
    run_id: str,
    analysis_date: date,
    trigger_type: str,
    status: str,
    report_markdown: str,
    sections: dict[str, Any],
    question: str | None = None,
    answer_markdown: str | None = None,
) -> None:
    """Persist one system-analysis run.

    Args:
        run_id: Unique run identifier.
        analysis_date: Local date represented by the report.
        trigger_type: Manual/scheduled/recovery trigger metadata.
        status: Final run status.
        report_markdown: Rendered Markdown report.
        sections: Structured JSON sections behind the report.
        question: Optional chat question answered during the run.
        answer_markdown: Optional deterministic answer.

    Side Effects:
        Upserts the run row in DuckDB.
    """
    ensure_system_analysis_tables()
    with get_connection(read_only=False) as conn:
        conn.execute(
            """
            INSERT INTO agent_system_analysis_runs (
                run_id,
                analysis_date,
                trigger_type,
                status,
                report_markdown,
                sections_json,
                question,
                answer_markdown
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (run_id) DO UPDATE SET
                status = excluded.status,
                report_markdown = excluded.report_markdown,
                sections_json = excluded.sections_json,
                question = excluded.question,
                answer_markdown = excluded.answer_markdown
            """,
            [
                run_id,
                analysis_date,
                trigger_type,
                status,
                report_markdown,
                json.dumps(sections, ensure_ascii=True, default=_json_default, sort_keys=True),
                question,
                answer_markdown,
            ],
        )


def load_latest_system_analysis() -> dict[str, Any] | None:
    """Load the latest successful system-analysis report.

    Returns:
        Dictionary with report metadata and parsed sections, or `None`.
    """
    ensure_system_analysis_tables()
    with get_connection(read_only=True) as conn:
        row = conn.execute(
            """
            SELECT run_id, analysis_date, trigger_type, status, report_markdown, sections_json,
                   question, answer_markdown, created_at
            FROM v_latest_agent_system_analysis
            """
        ).fetchone()
    if not row:
        return None
    return {
        "run_id": row[0],
        "analysis_date": row[1],
        "trigger_type": row[2],
        "status": row[3],
        "report_markdown": row[4],
        "sections": json.loads(row[5]),
        "question": row[6],
        "answer_markdown": row[7],
        "created_at": row[8],
    }


def answer_latest_system_question(question: str) -> str:
    """Answer a UI chat question using the latest stored analysis.

    Args:
        question: User-entered question.

    Returns:
        Markdown answer. If no report exists, the answer explains the required
        action instead of running a hidden side effect.
    """
    latest = load_latest_system_analysis()
    if latest is None:
        return "No system analysis is available yet. Run System Analysis first."
    return _answer_from_sections(question, latest["sections"])


def run_system_analysis(
    *,
    trigger_type: str = "manual",
    question: str | None = None,
    refresh_recommendations: bool = False,
) -> dict[str, Any]:
    """Run all deterministic BQuant system agents and persist a report.

    Args:
        trigger_type: Manual/scheduled/recovery trigger metadata.
        question: Optional chat question to answer from the generated report.
        refresh_recommendations: Whether to run the recommendation agent cycle
            before portfolio summarization.

    Returns:
        Run summary with run id, status, and rendered report.

    Raises:
        Exception: Re-raises failures after logging and pipeline-run recording.
    """
    ensure_refresh_state_table()
    ensure_system_analysis_tables()
    run_id = create_run_id()
    start_time = datetime.now()
    logger = BQuantLogger(
        PIPELINE_NAME,
        component="agent",
        subcomponent=PIPELINE_NAME,
        default_channel="pipeline",
    ).with_run_context(run_id=run_id, trigger_type=trigger_type, dataset_name=DATASET_NAME)
    steps_completed: list[str] = []
    steps_failed: list[str] = []
    report_markdown = ""
    answer_markdown = None

    try:
        logger.info(
            "Starting BQuant system analysis agents",
            event_type="job_start",
            status="running",
            refresh_recommendations=refresh_recommendations,
            has_question=bool(question),
        )
        data_health = collect_data_health()
        steps_completed.append("data_health_agent")
        chart_ta = collect_chart_ta_summary()
        steps_completed.append("chart_ta_agent")
        portfolio = collect_portfolio_summary(
            refresh_recommendations=refresh_recommendations,
            trigger_type=trigger_type,
        )
        steps_completed.append("portfolio_optimizer_agent")
        llm_runtime = load_llm_runtime_plan()
        steps_completed.append("llm_runtime_planner")

        sections = {
            "analysis_date": now_local().date().isoformat(),
            "agent_version": AGENT_VERSION,
            "data_health": data_health,
            "chart_ta": chart_ta,
            "portfolio": portfolio,
            "llm_runtime": llm_runtime,
        }
        report_markdown = _render_report(sections)
        if question:
            answer_markdown = _answer_from_sections(question, sections)
            steps_completed.append("answer_question")

        record_system_analysis_run(
            run_id=run_id,
            analysis_date=now_local().date(),
            trigger_type=trigger_type,
            status="success",
            report_markdown=report_markdown,
            sections=sections,
            question=question,
            answer_markdown=answer_markdown,
        )
        steps_completed.append("record_system_analysis")

        refresh_state = bump_refresh_version(
            DATASET_NAME,
            run_id=run_id,
            latest_data_ts=datetime.combine(now_local().date(), time.min),
            last_success_at=datetime.now(),
        )
        steps_completed.append("refresh_version")

        finished_at = datetime.now()
        record_pipeline_run(
            run_id=run_id,
            pipeline_name=PIPELINE_NAME,
            start_time=start_time,
            end_time=finished_at,
            status="success",
            input_rows=0,
            output_rows=1,
        )
        logger.log_pipeline_run(
            pipeline_name=PIPELINE_NAME,
            start_time=start_time.isoformat(),
            end_time=finished_at.isoformat(),
            status="success",
            steps_completed=steps_completed,
            steps_failed=steps_failed,
            run_id=run_id,
            trigger_type=trigger_type,
            dataset_name=DATASET_NAME,
            refresh_version=refresh_state["refresh_version"],
            data_health_status=data_health["status"],
            portfolio_status=portfolio["status"],
        )
        return {
            "run_id": run_id,
            "status": "success",
            "report_markdown": report_markdown,
            "answer_markdown": answer_markdown,
            "refresh_version": refresh_state["refresh_version"],
        }
    except Exception as exc:
        steps_failed.append("run_system_analysis")
        finished_at = datetime.now()
        record_pipeline_run(
            run_id=run_id,
            pipeline_name=PIPELINE_NAME,
            start_time=start_time,
            end_time=finished_at,
            status="failed",
            input_rows=0,
            output_rows=0,
            error_message=str(exc),
        )
        logger.log_error(
            PIPELINE_NAME,
            type(exc).__name__,
            str(exc),
            context={
                "run_id": run_id,
                "trigger_type": trigger_type,
                "steps_completed": steps_completed,
                "steps_failed": steps_failed,
            },
            channel="pipeline",
        )
        logger.log_pipeline_run(
            pipeline_name=PIPELINE_NAME,
            start_time=start_time.isoformat(),
            end_time=finished_at.isoformat(),
            status="failed",
            steps_completed=steps_completed,
            steps_failed=steps_failed,
            error_message=str(exc),
            run_id=run_id,
            trigger_type=trigger_type,
            dataset_name=DATASET_NAME,
        )
        raise
