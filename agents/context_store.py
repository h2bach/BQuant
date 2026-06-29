"""Read dbt-produced context rows for BQuant agents."""

from __future__ import annotations

from datetime import date
from typing import Any

import pandas as pd

from agents.config import load_agent_config, runtime_config
from warehouse.duckdb_connection import get_connection


DEFAULT_CONTEXT_TABLE = "analytics_marts.mart_agent_context_daily"


def resolve_context_table(config: dict[str, Any] | None = None) -> str:
    """Resolve the source mart table used by the agent cycle.

    Args:
        config: Optional preloaded agent configuration.

    Returns:
        Fully qualified DuckDB table name.
    """
    return str(runtime_config(config).get("context_table", DEFAULT_CONTEXT_TABLE))


def latest_context_date(context_table: str | None = None) -> date | None:
    """Return the latest trading date available in the context mart.

    Args:
        context_table: Optional fully qualified table name.

    Returns:
        Latest trading date, or `None` when the mart is empty/missing.
    """
    table_name = context_table or DEFAULT_CONTEXT_TABLE
    try:
        with get_connection(read_only=True) as conn:
            value = conn.execute(f"SELECT max(trading_date) FROM {table_name}").fetchone()[0]
    except Exception:
        return None
    if value is None:
        return None
    return pd.Timestamp(value).date()


def load_agent_context(
    *,
    as_of_date: date | str | None = None,
    limit: int | None = None,
    config: dict[str, Any] | None = None,
) -> pd.DataFrame:
    """Load agent-ready rows from the dbt context mart.

    Args:
        as_of_date: Optional target trading date. When omitted, the latest
            available mart date is used.
        limit: Optional row limit for smoke tests or focused runs.
        config: Optional preloaded agent configuration.

    Returns:
        DataFrame ordered by liquidity percentile and symbol.

    Raises:
        RuntimeError: If no context rows are available.
    """
    loaded_config = config or load_agent_config()
    table_name = resolve_context_table(loaded_config)
    target_date = pd.Timestamp(as_of_date).date() if as_of_date else latest_context_date(table_name)
    if target_date is None:
        raise RuntimeError(f"No agent context rows available in {table_name}")

    sql = f"""
        SELECT *
        FROM {table_name}
        WHERE trading_date = ?
        ORDER BY traded_value_cross_section_percentile DESC NULLS LAST, symbol
    """
    if limit is not None:
        sql += " LIMIT ?"
        params: list[Any] = [target_date, int(limit)]
    else:
        params = [target_date]

    with get_connection(read_only=True) as conn:
        frame = conn.execute(sql, params).df()
    if frame.empty:
        raise RuntimeError(f"No agent context rows available for trading_date={target_date}")
    return frame
