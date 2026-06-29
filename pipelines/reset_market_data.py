"""Reset BQuant market data tables, manifest rows, refresh state, and parquet files."""

from __future__ import annotations

import argparse
import shutil
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from utils.logger import BQuantLogger
from warehouse.duckdb_connection import get_connection


REPO_ROOT = Path(__file__).resolve().parent.parent
PIPELINE_NAME = "reset_market_data"
DAILY_DATASETS = ["daily_ohlcv_10y", "market_index_daily_10y"]
INTRADAY_DATASETS = ["intraday_ohlcv_15m_60d", "intraday_ohlcv_15m_delta"]
DAILY_TABLES = [
    "daily_ohlcv_base",
    "market_index_daily_base",
    "technical_features_daily",
    "combined_features_daily",
    "trading_signals",
]
INTRADAY_TABLES = ["intraday_ohlcv_15m_base", "intraday_ohlcv_15m_delta"]
DATASET_PATHS = {
    "daily_ohlcv_10y": REPO_ROOT / "data" / "base" / "daily_10y",
    "market_index_daily_10y": REPO_ROOT / "data" / "base" / "market_index_daily_10y",
    "intraday_ohlcv_15m_60d": REPO_ROOT / "data" / "base" / "intraday_15m_60d",
    "intraday_ohlcv_15m_delta": REPO_ROOT / "data" / "base" / "intraday_15m_delta",
}
MANIFEST_FILE = REPO_ROOT / "data" / "metadata" / "data_file_manifest.parquet"


def parse_args() -> argparse.Namespace:
    """Parse reset flags with explicit destructive-operation confirmation.

    Returns:
        Namespace with `daily`, `intraday`, and `confirm` booleans.
    """
    parser = argparse.ArgumentParser(description="Reset BQuant market data stores.")
    parser.add_argument("--daily", action="store_true", help="Reset daily stock/index datasets.")
    parser.add_argument("--intraday", action="store_true", help="Reset intraday base and delta datasets.")
    parser.add_argument("--confirm", action="store_true", help="Required confirmation for deletion.")
    return parser.parse_args()


def _table_exists(conn: Any, table_name: str) -> bool:
    """Check whether a DuckDB table exists in the main schema.

    Args:
        conn: Open DuckDB connection.
        table_name: Table name to check.

    Returns:
        True when the table exists; otherwise false.
    """
    row = conn.execute(
        """
        SELECT count(*)
        FROM information_schema.tables
        WHERE table_schema = 'main'
          AND table_name = ?
        """,
        [table_name],
    ).fetchone()
    return bool(row and row[0])


def _truncate_table(conn: Any, table_name: str) -> int:
    """Delete all rows from an existing table and report removed rows.

    Args:
        conn: Open writable DuckDB connection.
        table_name: Table to truncate.

    Returns:
        Number of rows that existed before deletion. Missing tables return 0.

    Side Effects:
        Executes `DELETE FROM <table_name>` for existing tables.
    """
    if not _table_exists(conn, table_name):
        return 0
    row_count = int(conn.execute(f"SELECT count(*) FROM {table_name}").fetchone()[0])
    conn.execute(f"DELETE FROM {table_name}")
    return row_count


def _delete_dataset_rows(conn: Any, datasets: list[str]) -> dict[str, int]:
    """Delete metadata rows for selected datasets.

    Args:
        conn: Open writable DuckDB connection.
        datasets: Dataset names to remove from manifest and refresh state.

    Returns:
        Mapping from metadata table name to deleted row count.

    Side Effects:
        Deletes matching rows from `data_file_manifest` and
        `dataset_refresh_state` when those tables exist.
    """
    deleted: dict[str, int] = {}
    if not datasets:
        return deleted
    placeholders = ",".join(["?"] * len(datasets))
    if _table_exists(conn, "data_file_manifest"):
        count = int(
            conn.execute(
                f"SELECT count(*) FROM data_file_manifest WHERE dataset_name IN ({placeholders})",
                datasets,
            ).fetchone()[0]
        )
        conn.execute(f"DELETE FROM data_file_manifest WHERE dataset_name IN ({placeholders})", datasets)
        deleted["data_file_manifest"] = count
    if _table_exists(conn, "dataset_refresh_state"):
        count = int(
            conn.execute(
                f"SELECT count(*) FROM dataset_refresh_state WHERE dataset_name IN ({placeholders})",
                datasets,
            ).fetchone()[0]
        )
        conn.execute(f"DELETE FROM dataset_refresh_state WHERE dataset_name IN ({placeholders})", datasets)
        deleted["dataset_refresh_state"] = count
    return deleted


def _reset_directory(path: Path) -> int:
    """Remove and recreate a dataset parquet directory.

    Args:
        path: Directory to clear.

    Returns:
        Number of files removed. Missing directories are created and return 0.

    Side Effects:
        Deletes the directory tree at `path` when present.
    """
    if not path.exists():
        path.mkdir(parents=True, exist_ok=True)
        return 0
    removed = sum(1 for item in path.rglob("*") if item.is_file())
    shutil.rmtree(path)
    path.mkdir(parents=True, exist_ok=True)
    return removed


def reset_market_data(*, reset_daily: bool, reset_intraday: bool) -> dict[str, Any]:
    """Delete selected market datasets and return deletion counts.

    Args:
        reset_daily: Whether to clear daily stock/index tables and parquet
            datasets.
        reset_intraday: Whether to clear intraday base/delta tables and parquet
            datasets.

    Returns:
        Dictionary containing deleted table row counts, metadata row counts,
        parquet file counts, and whether the manifest parquet file was removed.

    Raises:
        ValueError: If neither daily nor intraday reset is requested.

    Side Effects:
        Deletes rows from warehouse tables, removes dataset parquet files, and
        deletes the materialized manifest parquet file.
    """
    datasets: list[str] = []
    tables: list[str] = []
    if reset_daily:
        datasets.extend(DAILY_DATASETS)
        tables.extend(DAILY_TABLES)
    if reset_intraday:
        datasets.extend(INTRADAY_DATASETS)
        tables.extend(INTRADAY_TABLES)
    if not datasets:
        raise ValueError("At least one of --daily or --intraday is required")

    result: dict[str, Any] = {"tables": {}, "metadata_rows": {}, "paths": {}, "manifest_file_removed": False}
    with get_connection(read_only=False) as conn:
        for table_name in tables:
            result["tables"][table_name] = _truncate_table(conn, table_name)
        result["metadata_rows"] = _delete_dataset_rows(conn, datasets)

    for dataset_name in datasets:
        result["paths"][str(DATASET_PATHS[dataset_name])] = _reset_directory(DATASET_PATHS[dataset_name])

    if MANIFEST_FILE.exists():
        MANIFEST_FILE.unlink()
        result["manifest_file_removed"] = True
    return result


def main() -> None:
    """CLI entrypoint for guarded market-data reset.

    Raises:
        SystemExit: If `--confirm` is omitted.

    Side Effects:
        Executes the selected reset, writes structured pipeline logs, and
        re-raises unexpected failures after logging them.
    """
    args = parse_args()
    if not args.confirm:
        raise SystemExit("Refusing to reset market data without --confirm")

    logger = BQuantLogger(PIPELINE_NAME, component="pipeline", subcomponent=PIPELINE_NAME)
    run_id = str(uuid.uuid4())
    started_at = datetime.now()
    try:
        result = reset_market_data(reset_daily=args.daily, reset_intraday=args.intraday)
        finished_at = datetime.now()
        logger.log_pipeline_run(
            pipeline_name=PIPELINE_NAME,
            start_time=started_at.isoformat(),
            end_time=finished_at.isoformat(),
            status="success",
            steps_completed=["reset_market_data"],
            steps_failed=[],
            run_id=run_id,
            reset_daily=args.daily,
            reset_intraday=args.intraday,
            deletion_summary=result,
        )
    except Exception as exc:
        finished_at = datetime.now()
        logger.log_error(
            PIPELINE_NAME,
            type(exc).__name__,
            str(exc),
            context={"run_id": run_id, "reset_daily": args.daily, "reset_intraday": args.intraday},
        )
        logger.log_pipeline_run(
            pipeline_name=PIPELINE_NAME,
            start_time=started_at.isoformat(),
            end_time=finished_at.isoformat(),
            status="failed",
            steps_completed=[],
            steps_failed=["reset_market_data"],
            error_message=str(exc),
            run_id=run_id,
        )
        raise


if __name__ == "__main__":
    main()
