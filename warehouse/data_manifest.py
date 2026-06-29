"""Symbol-file materialization and manifest management for base datasets."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
import yaml

from warehouse.duckdb_connection import get_connection


REPO_ROOT = Path(__file__).resolve().parent.parent
DATASET_REGISTRY_PATH = REPO_ROOT / "configs" / "dataset_registry.yaml"
DATA_MANAGEMENT_PATH = REPO_ROOT / "configs" / "data_management.yaml"
BQUANT_CONFIG_PATH = REPO_ROOT / "configs" / "bquant.yaml"


@dataclass(frozen=True)
class DatasetFileSpec:
    dataset_name: str
    table_name: str
    time_column: str
    granularity: str
    interval: str | None
    file_role: str
    source: str
    uses_snapshot_date: bool
    needs_merge: bool
    export_columns: list[str]


DATASET_FILE_SPECS: dict[str, DatasetFileSpec] = {
    "daily_ohlcv_10y": DatasetFileSpec(
        dataset_name="daily_ohlcv_10y",
        table_name="daily_ohlcv_base",
        time_column="trading_date",
        granularity="daily",
        interval="1d",
        file_role="base",
        source="vnstock:vci",
        uses_snapshot_date=False,
        needs_merge=False,
        export_columns=[
            "symbol",
            "trading_date",
            "open",
            "high",
            "low",
            "close",
            "adjusted_close",
            "volume",
            "source",
        ],
    ),
    "market_index_daily_10y": DatasetFileSpec(
        dataset_name="market_index_daily_10y",
        table_name="market_index_daily_base",
        time_column="trading_date",
        granularity="daily",
        interval="1d",
        file_role="base",
        source="vnstock:vci",
        uses_snapshot_date=False,
        needs_merge=False,
        export_columns=[
            "symbol",
            "trading_date",
            "open",
            "high",
            "low",
            "close",
            "adjusted_close",
            "volume",
            "source",
        ],
    ),
    "intraday_ohlcv_15m_60d": DatasetFileSpec(
        dataset_name="intraday_ohlcv_15m_60d",
        table_name="intraday_ohlcv_15m_base",
        time_column="bar_time",
        granularity="intraday",
        interval="15m",
        file_role="base",
        source="vnstock:vci",
        uses_snapshot_date=True,
        needs_merge=False,
        export_columns=[
            "symbol",
            "bar_time",
            "session_date",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "interval",
            "source",
            "snapshot_date",
        ],
    ),
    "intraday_ohlcv_15m_delta": DatasetFileSpec(
        dataset_name="intraday_ohlcv_15m_delta",
        table_name="intraday_ohlcv_15m_delta",
        time_column="bar_time",
        granularity="intraday",
        interval="15m",
        file_role="delta",
        source="vnstock:vci",
        uses_snapshot_date=True,
        needs_merge=True,
        export_columns=[
            "symbol",
            "bar_time",
            "session_date",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "interval",
            "source",
            "snapshot_date",
        ],
    ),
}


def _load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def _load_dataset_registry(dataset_name: str) -> dict[str, Any]:
    registry = _load_yaml(DATASET_REGISTRY_PATH)
    datasets = registry.get("datasets", {})
    if dataset_name not in datasets:
        raise KeyError(f"Dataset {dataset_name} is not registered in {DATASET_REGISTRY_PATH}")
    return datasets[dataset_name]


def _load_management_config(dataset_name: str) -> dict[str, Any]:
    management = _load_yaml(DATA_MANAGEMENT_PATH)
    datasets = management.get("datasets", {})
    if dataset_name not in datasets:
        raise KeyError(f"Dataset {dataset_name} is not managed in {DATA_MANAGEMENT_PATH}")
    return datasets[dataset_name]


def _load_parallel_workers(dataset_name: str) -> int:
    bquant_cfg = _load_yaml(BQUANT_CONFIG_PATH)
    execution_workers = int(bquant_cfg.get("execution", {}).get("parallel_workers", 4))
    managed_cfg = _load_management_config(dataset_name)
    return max(int(managed_cfg.get("parallel_fetch_workers", execution_workers)), 1)


def _resolve_dataset_path(dataset_name: str) -> Path:
    config = _load_dataset_registry(dataset_name)
    raw_path = Path(config["parquet_path"])
    if raw_path.is_absolute():
        return raw_path
    return (REPO_ROOT / raw_path).resolve()


def _manifest_path() -> Path:
    config = _load_dataset_registry("data_file_manifest")
    raw_path = Path(config["parquet_path"])
    if raw_path.is_absolute():
        return raw_path
    return (REPO_ROOT / raw_path).resolve()


def _format_token(value: datetime | date) -> str:
    if isinstance(value, datetime):
        return value.strftime("%Y%m%d")
    return value.strftime("%Y%m%d")


def _format_file_name(spec: DatasetFileSpec, symbol: str, df: pd.DataFrame, snapshot_date: date | None) -> str:
    dataset_cfg = _load_dataset_registry(spec.dataset_name)
    pattern = str(dataset_cfg.get("file_pattern"))
    coverage_start = pd.to_datetime(df[spec.time_column].min()).date()

    values = {
        "symbol": symbol,
        "coverage_start": _format_token(coverage_start),
        "snapshot_date": _format_token(snapshot_date or datetime.now().date()),
    }
    return pattern.format(**values)


def _cleanup_existing_files(directory: Path, symbol: str, spec: DatasetFileSpec, target_name: str) -> None:
    if spec.granularity == "daily":
        pattern = f"{symbol}_*.parquet"
    elif spec.dataset_name == "intraday_ohlcv_15m_60d":
        pattern = f"{symbol}_intra60_*.parquet"
    else:
        pattern = f"{symbol}_intra_delta_*.parquet"

    for candidate in directory.glob(pattern):
        if candidate.name != target_name:
            candidate.unlink(missing_ok=True)


def _delete_symbol_files(directory: Path, symbol: str, spec: DatasetFileSpec) -> None:
    if spec.granularity == "daily":
        pattern = f"{symbol}_*.parquet"
    elif spec.dataset_name == "intraday_ohlcv_15m_60d":
        pattern = f"{symbol}_intra60_*.parquet"
    else:
        pattern = f"{symbol}_intra_delta_*.parquet"
    for candidate in directory.glob(pattern):
        candidate.unlink(missing_ok=True)


def _latest_expected_ts(spec: DatasetFileSpec, snapshot_date: date | None) -> datetime | None:
    managed_cfg = _load_management_config(spec.dataset_name)
    refresh_time = managed_cfg.get("refresh_time")
    if spec.granularity == "daily":
        if not refresh_time:
            return None
        hour, minute = [int(part) for part in str(refresh_time).split(":", maxsplit=1)]
        today_close = datetime.combine(datetime.now().date(), time(hour=hour, minute=minute))
        if datetime.now() < today_close:
            return today_close
        return today_close

    if spec.file_role == "base":
        base_date = snapshot_date or datetime.now().date()
        return datetime.combine(base_date, time(hour=15, minute=0))

    return datetime.now()


def _infer_status(spec: DatasetFileSpec, coverage_end: datetime, latest_expected: datetime | None) -> str:
    if spec.needs_merge:
        return "pending_merge"
    if latest_expected is None:
        return "unknown"

    now = datetime.now()
    if spec.granularity == "daily":
        if now < latest_expected and coverage_end.date() < latest_expected.date():
            return "awaiting_refresh_window"
        return "up_to_date" if coverage_end.date() >= latest_expected.date() else "stale"

    if now < latest_expected and coverage_end < latest_expected:
        return "awaiting_refresh_window"
    freshness_minutes = int(
        _load_yaml(DATA_MANAGEMENT_PATH)
        .get("manifest", {})
        .get("freshness", {})
        .get("intraday_max_lag_minutes", 30)
    )
    return "up_to_date" if latest_expected - coverage_end <= timedelta(minutes=freshness_minutes) else "stale"


def _read_table_slice(spec: DatasetFileSpec, symbol: str) -> pd.DataFrame:
    select_columns = ", ".join(spec.export_columns)
    with get_connection(read_only=True) as conn:
        return conn.execute(
            f"""
            SELECT {select_columns}
            FROM {spec.table_name}
            WHERE symbol = ?
            ORDER BY {spec.time_column}
            """,
            [symbol],
        ).df()


def _upsert_manifest_row(record: dict[str, Any]) -> None:
    columns = [
        "dataset_name",
        "symbol",
        "file_role",
        "granularity",
        "interval",
        "file_name",
        "file_path",
        "coverage_start",
        "coverage_end",
        "row_count",
        "file_size_bytes",
        "snapshot_date",
        "source",
        "latest_expected_ts",
        "update_status",
        "needs_merge",
        "notes",
        "last_refresh_at",
    ]
    values = [record.get(column) for column in columns]
    placeholders = ", ".join(["?"] * len(columns))
    assignments = ", ".join([f"{column} = excluded.{column}" for column in columns[3:]])

    with get_connection(read_only=False) as conn:
        conn.execute(
            f"""
            INSERT INTO data_file_manifest ({", ".join(columns)})
            VALUES ({placeholders})
            ON CONFLICT (dataset_name, symbol, file_role) DO UPDATE SET
            {assignments}
            """,
            values,
        )


def _record_empty_manifest_row(spec: DatasetFileSpec, symbol: str, snapshot_date: date | None) -> dict[str, Any]:
    output_dir = _resolve_dataset_path(spec.dataset_name)
    output_dir.mkdir(parents=True, exist_ok=True)
    _delete_symbol_files(output_dir, symbol, spec)
    record = {
        "dataset_name": spec.dataset_name,
        "symbol": symbol,
        "file_role": spec.file_role,
        "granularity": spec.granularity,
        "interval": spec.interval,
        "file_name": "",
        "file_path": "",
        "coverage_start": None,
        "coverage_end": None,
        "row_count": 0,
        "file_size_bytes": 0,
        "snapshot_date": snapshot_date,
        "source": spec.source,
        "latest_expected_ts": _latest_expected_ts(spec, snapshot_date),
        "update_status": "empty",
        "needs_merge": False,
        "notes": "No rows materialized for symbol",
        "last_refresh_at": datetime.now(),
    }
    _upsert_manifest_row(record)
    return record


def export_manifest_file() -> str:
    output_path = _manifest_path()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with get_connection(read_only=True) as conn:
        manifest_df = conn.execute(
            """
            SELECT *
            FROM data_file_manifest
            ORDER BY dataset_name, symbol, file_role
            """
        ).df()
    manifest_df.to_parquet(output_path, index=False)
    return str(output_path)


def _materialize_symbol(spec: DatasetFileSpec, symbol: str, snapshot_date: date | None) -> dict[str, Any] | None:
    df = _read_table_slice(spec, symbol)
    if df.empty:
        return _record_empty_manifest_row(spec, symbol, snapshot_date)

    output_dir = _resolve_dataset_path(spec.dataset_name)
    output_dir.mkdir(parents=True, exist_ok=True)
    file_name = _format_file_name(spec, symbol, df, snapshot_date)
    _cleanup_existing_files(output_dir, symbol, spec, file_name)

    file_path = output_dir / file_name
    df.to_parquet(file_path, index=False)

    coverage_start = pd.to_datetime(df[spec.time_column].min()).to_pydatetime()
    coverage_end = pd.to_datetime(df[spec.time_column].max()).to_pydatetime()
    latest_expected = _latest_expected_ts(spec, snapshot_date)

    record = {
        "dataset_name": spec.dataset_name,
        "symbol": symbol,
        "file_role": spec.file_role,
        "granularity": spec.granularity,
        "interval": spec.interval,
        "file_name": file_name,
        "file_path": str(file_path),
        "coverage_start": coverage_start,
        "coverage_end": coverage_end,
        "row_count": int(len(df)),
        "file_size_bytes": int(file_path.stat().st_size),
        "snapshot_date": snapshot_date,
        "source": spec.source,
        "latest_expected_ts": latest_expected,
        "update_status": _infer_status(spec, coverage_end, latest_expected),
        "needs_merge": spec.needs_merge,
        "notes": None,
        "last_refresh_at": datetime.now(),
    }
    _upsert_manifest_row(record)
    return record


def materialize_dataset_from_table(
    dataset_name: str,
    *,
    symbols: list[str] | None = None,
    snapshot_date: date | None = None,
    max_workers: int | None = None,
) -> list[dict[str, Any]]:
    if dataset_name not in DATASET_FILE_SPECS:
        raise KeyError(f"Dataset {dataset_name} is not materializable")

    spec = DATASET_FILE_SPECS[dataset_name]
    if symbols is None:
        with get_connection(read_only=True) as conn:
            rows = conn.execute(
                f"SELECT DISTINCT symbol FROM {spec.table_name} ORDER BY symbol"
            ).fetchall()
        symbols = [str(row[0]) for row in rows]

    records: list[dict[str, Any]] = []
    # DuckDB rejects mixing read_only and read_write connections to the same
    # file when they overlap. Materialization does DB reads and manifest upserts,
    # so keep this phase serialized while fetch/upsert remains parallel upstream.
    for symbol in symbols:
        result = _materialize_symbol(spec, symbol, snapshot_date)
        if result is not None:
            records.append(result)

    export_manifest_file()
    return sorted(records, key=lambda item: (item["dataset_name"], item["symbol"]))


def clear_dataset_materialization(
    dataset_name: str,
    *,
    symbols: list[str] | None = None,
    snapshot_date: date | None = None,
) -> list[dict[str, Any]]:
    if dataset_name not in DATASET_FILE_SPECS:
        raise KeyError(f"Dataset {dataset_name} is not materializable")
    spec = DATASET_FILE_SPECS[dataset_name]
    if symbols is None:
        with get_connection(read_only=True) as conn:
            rows = conn.execute(
                """
                SELECT DISTINCT symbol
                FROM universe_members
                ORDER BY symbol
                """
            ).fetchall()
        symbols = [str(row[0]) for row in rows]

    records = [_record_empty_manifest_row(spec, symbol, snapshot_date) for symbol in symbols]
    export_manifest_file()
    return records


def initialize_manifest_file() -> str:
    output_path = _manifest_path()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.exists():
        return str(output_path)
    with get_connection(read_only=True) as conn:
        empty_df = conn.execute("SELECT * FROM data_file_manifest WHERE 1 = 0").df()
    empty_df.to_parquet(output_path, index=False)
    return str(output_path)
