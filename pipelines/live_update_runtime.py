"""Shared runtime helpers for BQuant live update pipelines."""

from __future__ import annotations

import uuid
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
import yaml

from data_ingestion.fetch_vn30_universe import DEFAULT_CONFIG_PATH
from warehouse.duckdb_connection import get_connection


REPO_ROOT = Path(__file__).resolve().parent.parent
LIVE_UPDATE_CONFIG_PATH = REPO_ROOT / "configs" / "live_update.yaml"


def _load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def load_live_update_config() -> dict[str, Any]:
    return _load_yaml(LIVE_UPDATE_CONFIG_PATH)


def market_timezone() -> ZoneInfo:
    timezone_name = str(load_live_update_config().get("timezone", "Asia/Ho_Chi_Minh"))
    return ZoneInfo(timezone_name)


def now_local() -> datetime:
    return datetime.now(tz=market_timezone())


def parse_hhmm(value: str) -> time:
    hour, minute = [int(part) for part in value.split(":", maxsplit=1)]
    return time(hour=hour, minute=minute)


def intraday_slot_strings() -> list[str]:
    return list(load_live_update_config().get("market", {}).get("intraday_slots", []))


def intraday_slot_datetimes(trade_date: date) -> list[datetime]:
    tz = market_timezone()
    return [
        datetime.combine(trade_date, parse_hhmm(slot)).replace(tzinfo=tz)
        for slot in intraday_slot_strings()
    ]


def eod_reconcile_time(trade_date: date) -> datetime:
    tz = market_timezone()
    hhmm = str(load_live_update_config().get("market", {}).get("eod_reconcile_time", "15:10"))
    return datetime.combine(trade_date, parse_hhmm(hhmm)).replace(tzinfo=tz)


def is_trading_day(moment: datetime | date) -> bool:
    if isinstance(moment, datetime):
        current_date = moment.date()
    else:
        current_date = moment
    weekend_days = set(load_live_update_config().get("market", {}).get("weekend_days", [5, 6]))
    return current_date.weekday() not in weekend_days


def market_session_state(moment: datetime | None = None) -> str:
    current = moment or now_local()
    if not is_trading_day(current):
        return "market_closed"
    slots = intraday_slot_datetimes(current.date())
    if not slots:
        return "market_closed"
    if current < slots[0]:
        return "pre_market"
    if current > eod_reconcile_time(current.date()):
        return "post_close"
    morning_end = datetime.combine(current.date(), time(11, 30)).replace(tzinfo=market_timezone())
    afternoon_start = datetime.combine(current.date(), time(13, 0)).replace(tzinfo=market_timezone())
    if current <= morning_end:
        return "intraday_morning"
    if current < afternoon_start:
        return "lunch_break"
    return "intraday_afternoon"


def latest_eligible_intraday_slot(moment: datetime | None = None) -> datetime | None:
    current = moment or now_local()
    if not is_trading_day(current):
        return None
    eligible = [slot for slot in intraday_slot_datetimes(current.date()) if slot <= current]
    return eligible[-1] if eligible else None


def recent_due_intraday_slots(moment: datetime | None = None, catch_up_slots: int | None = None) -> list[datetime]:
    current = moment or now_local()
    eligible = [slot for slot in intraday_slot_datetimes(current.date()) if slot <= current]
    if not eligible:
        return []
    configured = int(load_live_update_config().get("worker", {}).get("catch_up_slots", 2))
    slot_count = max(int(catch_up_slots or configured), 1)
    return eligible[-slot_count:]


def resolve_universe_symbols(*, explicit_symbols: list[str] | None = None, use_test: bool = False) -> list[str]:
    if explicit_symbols:
        return [str(symbol).upper() for symbol in explicit_symbols]
    payload = _load_yaml(DEFAULT_CONFIG_PATH)
    key = "test_symbols" if use_test else "symbols"
    symbols = payload.get(key, [])
    if not symbols:
        raise ValueError(f"No symbols found in {DEFAULT_CONFIG_PATH} for key={key}")
    return [str(symbol).upper() for symbol in symbols]


def create_run_id() -> str:
    return str(uuid.uuid4())


def record_pipeline_run(
    *,
    run_id: str,
    pipeline_name: str,
    start_time: datetime,
    end_time: datetime,
    status: str,
    input_rows: int,
    output_rows: int,
    error_message: str | None = None,
) -> None:
    with get_connection(read_only=False) as conn:
        conn.execute(
            """
            INSERT INTO pipeline_runs (
                run_id,
                pipeline_name,
                start_time,
                end_time,
                status,
                input_rows,
                output_rows,
                error_message
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (run_id) DO UPDATE SET
                end_time = excluded.end_time,
                status = excluded.status,
                input_rows = excluded.input_rows,
                output_rows = excluded.output_rows,
                error_message = excluded.error_message
            """,
            [run_id, pipeline_name, start_time, end_time, status, input_rows, output_rows, error_message],
        )


def get_latest_table_timestamp(table_name: str, column_name: str, symbol: str | None = None) -> datetime | None:
    sql = f"SELECT max({column_name}) FROM {table_name}"
    params: list[Any] = []
    if symbol is not None:
        sql += " WHERE symbol = ?"
        params.append(symbol)
    with get_connection(read_only=True) as conn:
        value = conn.execute(sql, params).fetchone()[0]
    if value is None:
        return None
    return pd.Timestamp(value).to_pydatetime()


def get_latest_plot_timestamp(symbol: str | None = None) -> datetime | None:
    return get_latest_table_timestamp("v_intraday_15m_plot_universe", "bar_time", symbol)


def get_plot_floor_timestamp() -> datetime | None:
    with get_connection(read_only=True) as conn:
        value = conn.execute(
            """
            SELECT min(max_bar_time)
            FROM (
                SELECT symbol, max(bar_time) AS max_bar_time
                FROM v_intraday_15m_plot_universe
                GROUP BY symbol
            )
            """
        ).fetchone()[0]
    if value is None:
        return None
    return pd.Timestamp(value).to_pydatetime()


def get_latest_daily_date() -> date | None:
    with get_connection(read_only=True) as conn:
        value = conn.execute("SELECT max(trading_date) FROM daily_ohlcv_base").fetchone()[0]
    if value is None:
        return None
    return pd.Timestamp(value).date()


def intraday_overlap_delta() -> timedelta:
    overlap_bars = int(load_live_update_config().get("jobs", {}).get("intraday_delta", {}).get("overlap_bars", 1))
    interval_minutes = int(load_live_update_config().get("market", {}).get("intraday_interval_minutes", 15))
    return timedelta(minutes=overlap_bars * interval_minutes)


def base_intraday_lookback_days() -> int:
    config = load_live_update_config()
    return int(config.get("jobs", {}).get("eod_reconcile", {}).get("lookback_days", 60))
