"""Fetch and materialize the canonical 60-day / delta 15m intraday datasets."""

from __future__ import annotations

import argparse
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
import yaml

from data_ingestion.fetch_vn30_universe import DEFAULT_CONFIG_PATH
from data_ingestion.vnstock_adapter import DEFAULT_SOURCE, DEFAULT_SOURCE_LABEL, fetch_intraday_history
from utils.logger import BQuantLogger
from utils.rate_limit import SlidingWindowRateLimiter
from warehouse.data_manifest import materialize_dataset_from_table
from warehouse.duckdb_connection import get_connection
from warehouse.refresh_state import bump_refresh_version


REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_SOURCES_CONFIG_PATH = REPO_ROOT / "configs" / "data_sources.yaml"
DATA_MANAGEMENT_PATH = REPO_ROOT / "configs" / "data_management.yaml"
PIPELINE_NAME = "fetch_intraday_15m"


def _load_yaml(path: Path) -> dict[str, Any]:
    """Load a YAML file into a dictionary.

    Args:
        path: YAML file path to read.

    Returns:
        Parsed mapping. Empty files return an empty dictionary.
    """
    with path.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def _source_config() -> dict[str, Any]:
    """Return the canonical vnstock source configuration block.

    Returns:
        `configs/data_sources.yaml` block keyed by `vnstock`.
    """
    return _load_yaml(DATA_SOURCES_CONFIG_PATH).get("vnstock", {})


def _management_config(mode: str) -> dict[str, Any]:
    """Return dataset-management settings for an intraday mode.

    Args:
        mode: `base` for 60-day base backfill or `delta` for rolling live
            update files.

    Returns:
        Dataset-management config block for the selected intraday dataset.
    """
    dataset_name = "intraday_ohlcv_15m_60d" if mode == "base" else "intraday_ohlcv_15m_delta"
    return _load_yaml(DATA_MANAGEMENT_PATH).get("datasets", {}).get(dataset_name, {})


def _default_base_start_date() -> str:
    """Return the default base backfill start timestamp.

    Returns:
        Timestamp string derived from the configured base lookback window.
    """
    lookback_days = int(_management_config("base").get("lookback_days", 60))
    start = datetime.now() - timedelta(days=lookback_days)
    return start.strftime("%Y-%m-%d %H:%M:%S")


def _default_end_date() -> str:
    """Return the current local timestamp as the default intraday end bound.

    Returns:
        Local timestamp string in `YYYY-MM-DD HH:MM:SS` form.
    """
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def parse_args() -> argparse.Namespace:
    """Parse CLI arguments for intraday base or delta ingestion.

    Returns:
        Namespace with mode, symbols/test flag, date bounds, and snapshot date.
    """
    parser = argparse.ArgumentParser(description="Fetch 15m intraday base or delta datasets.")
    parser.add_argument("--mode", choices=["base", "delta"], default="base")
    parser.add_argument("--symbols", nargs="*", help="Explicit symbols to fetch.")
    parser.add_argument("--test", action="store_true", help="Use the test universe.")
    parser.add_argument("--start-date", help="Start datetime YYYY-MM-DD HH:MM:SS.")
    parser.add_argument("--end-date", default=_default_end_date(), help="End datetime YYYY-MM-DD HH:MM:SS.")
    parser.add_argument("--snapshot-date", help="Snapshot date token for the output file names.")
    return parser.parse_args()


def _resolve_symbols(args: argparse.Namespace) -> list[str]:
    """Resolve the symbol list for the current intraday run.

    Args:
        args: Parsed CLI namespace containing optional `symbols` and `test`.

    Returns:
        Uppercase ticker list from CLI input or the configured VN30 universe.

    Raises:
        ValueError: If the selected universe config key has no symbols.
    """
    if args.symbols:
        return [str(symbol).upper() for symbol in args.symbols]
    payload = _load_yaml(DEFAULT_CONFIG_PATH)
    key = "test_symbols" if args.test else "symbols"
    symbols = payload.get(key, [])
    if not symbols:
        raise ValueError(f"No symbols found in universe config for key={key}")
    return [str(symbol).upper() for symbol in symbols]


def _get_latest_bar_time(symbol: str, table_name: str) -> datetime | None:
    """Return the latest saved intraday bar for one symbol/table pair.

    Args:
        symbol: Ticker to inspect.
        table_name: Intraday table name to query.

    Returns:
        Latest `bar_time` as a Python datetime, or `None` when no rows exist.

    Side Effects:
        Opens a read-only DuckDB connection.
    """
    with get_connection(read_only=True) as conn:
        value = conn.execute(
            f"SELECT max(bar_time) FROM {table_name} WHERE symbol = ?",
            [symbol],
        ).fetchone()[0]
    if value is None:
        return None
    return pd.Timestamp(value).to_pydatetime()


def _resolve_symbol_start(symbol: str, args: argparse.Namespace) -> str:
    """Resolve a symbol-specific start timestamp.

    Args:
        symbol: Ticker being fetched.
        args: Parsed CLI namespace containing mode and optional start date.

    Returns:
        Start timestamp string. Base mode defaults to the configured lookback;
        delta mode walks from latest delta bar, then latest base bar, then a
        one-day fallback.
    """
    if args.start_date:
        return args.start_date
    if args.mode == "base":
        return _default_base_start_date()

    # Delta mode walks forward from the most recent saved bar, preferring the delta
    # table first and falling back to the base table if the delta table is empty.
    latest_delta = _get_latest_bar_time(symbol, "intraday_ohlcv_15m_delta")
    if latest_delta is not None:
        return (latest_delta + timedelta(minutes=15)).strftime("%Y-%m-%d %H:%M:%S")

    latest_base = _get_latest_bar_time(symbol, "intraday_ohlcv_15m_base")
    if latest_base is not None:
        return (latest_base + timedelta(minutes=15)).strftime("%Y-%m-%d %H:%M:%S")

    return (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")


def _fetch_symbol(
    symbol: str,
    start_date: str,
    end_date: str,
    interval: str,
    limiter: SlidingWindowRateLimiter,
    logger: BQuantLogger,
) -> pd.DataFrame:
    """Fetch one symbol's intraday history with rate limiting and logging.

    Args:
        symbol: VN30 ticker to fetch.
        start_date: Inclusive start timestamp.
        end_date: Inclusive end timestamp.
        interval: Provider interval, normally `15m`.
        limiter: Shared provider rate limiter.
        logger: Structured logger for ingestion events.

    Returns:
        Standardized intraday OHLCV DataFrame, or an empty frame on provider
        failure/empty payload.

    Side Effects:
        May sleep for rate limiting and emits structured ingestion logs.
    """
    try:
        waited = limiter.acquire()
        if waited > 0:
            logger.info(
                "Paused to respect intraday source rate limit",
                pipeline_name=PIPELINE_NAME,
                symbol=symbol,
                wait_seconds=round(waited, 2),
            )
        chunk = fetch_intraday_history(symbol, start_date, end_date, interval=interval, source=DEFAULT_SOURCE)
        if chunk.empty:
            logger.warning(
                "Intraday 15m source returned no valid OHLCV bars",
                operation="fetch_intraday_15m",
                symbol=symbol,
                source=f"vnstock:{DEFAULT_SOURCE.lower()}",
                provider_label=DEFAULT_SOURCE_LABEL,
                start_date=start_date,
                end_date=end_date,
                interval=interval,
                status="empty",
            )
            return pd.DataFrame()

        logger.log_data_event(
            "ingestion",
            f"Fetched intraday 15m data for {symbol} via vnstock",
            operation="fetch_intraday_15m",
            symbol=symbol,
            source=f"vnstock:{DEFAULT_SOURCE.lower()}",
            provider_label=DEFAULT_SOURCE_LABEL,
            start_date=start_date,
            end_date=end_date,
            interval=interval,
            row_count=len(chunk),
            actual_start=str(chunk["bar_time"].min()),
            actual_end=str(chunk["bar_time"].max()),
        )
        return chunk
    except BaseException as exc:
        if isinstance(exc, KeyboardInterrupt):
            raise
        logger.warning(
            f"Intraday 15m fetch failed for {symbol}",
            operation="fetch_intraday_15m",
            symbol=symbol,
            source=f"vnstock:{DEFAULT_SOURCE.lower()}",
            provider_label=DEFAULT_SOURCE_LABEL,
            start_date=start_date,
            end_date=end_date,
            error_type=type(exc).__name__,
            error_message=str(exc),
        )
        return pd.DataFrame()


def _delete_existing_slice(conn, table_name: str, symbol: str, start_time: Any, end_time: Any) -> None:
    """Delete an intraday slice before inserting refreshed rows.

    Args:
        conn: Open writable DuckDB connection.
        table_name: Target intraday base or delta table.
        symbol: Ticker whose rows should be replaced.
        start_time: Inclusive slice start timestamp.
        end_time: Inclusive slice end timestamp.

    Side Effects:
        Deletes matching rows from `table_name`.
    """
    conn.execute(
        f"""
        DELETE FROM {table_name}
        WHERE symbol = ?
          AND bar_time BETWEEN ? AND ?
        """,
        [symbol, start_time, end_time],
    )


def upsert_intraday_rows(table_name: str, df: pd.DataFrame) -> int:
    """Replace affected intraday slices in the requested table.

    Args:
        table_name: `intraday_ohlcv_15m_base` or `intraday_ohlcv_15m_delta`.
        df: Standardized intraday OHLCV rows from `vnstock_adapter`.

    Returns:
        Number of rows inserted after slice deletion.

    Side Effects:
        Opens a writable DuckDB connection, deletes overlapping slices, and
        inserts refreshed rows.
    """
    if df.empty:
        return 0

    with get_connection(read_only=False) as conn:
        # Deleting the overlapping slice first keeps reruns idempotent for both
        # base backfills and rolling delta refreshes.
        for symbol, symbol_df in df.groupby("symbol", sort=True):
            _delete_existing_slice(
                conn,
                table_name,
                str(symbol),
                symbol_df["bar_time"].min(),
                symbol_df["bar_time"].max(),
            )
        conn.register("tmp_intraday_15m", df)
        conn.execute(
            f"""
            INSERT INTO {table_name} (
                symbol,
                bar_time,
                session_date,
                open,
                high,
                low,
                close,
                volume,
                interval,
                source,
                snapshot_date
            )
            SELECT
                symbol,
                bar_time,
                session_date,
                open,
                high,
                low,
                close,
                volume,
                interval,
                source,
                snapshot_date
            FROM tmp_intraday_15m
            """
        )
        conn.unregister("tmp_intraday_15m")
    return int(len(df))


def record_pipeline_run(
    *,
    status: str,
    start_time: datetime,
    end_time: datetime,
    input_rows: int,
    output_rows: int,
    error_message: str | None = None,
) -> None:
    """Persist one pipeline-run summary row.

    Args:
        status: Final status such as `success` or `failed`.
        start_time: Job start timestamp.
        end_time: Job end timestamp.
        input_rows: Number of fetched input rows.
        output_rows: Number of rows written/materialized.
        error_message: Optional failure message.

    Side Effects:
        Inserts into `pipeline_runs`.
    """
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
            """,
            [
                str(uuid.uuid4()),
                PIPELINE_NAME,
                start_time,
                end_time,
                status,
                input_rows,
                output_rows,
                error_message,
            ],
        )


def main() -> None:
    """Fetch, upsert, and materialize the configured intraday dataset.

    Side Effects:
        Fetches 15-minute bars from the vnstock `VCI-data-source` provider,
        writes intraday base/delta tables, materializes per-symbol parquet
        files, bumps refresh state, and writes pipeline logs.
    """
    args = parse_args()
    source_cfg = _source_config()
    mgmt_cfg = _management_config(args.mode)
    logger = BQuantLogger(PIPELINE_NAME)
    run_id = str(uuid.uuid4())

    interval = str(mgmt_cfg.get("interval", source_cfg.get("parameters", {}).get("intraday_interval", "15m")))
    max_workers = max(int(mgmt_cfg.get("parallel_fetch_workers", 2)), 1)
    requests_per_minute = int(source_cfg.get("rate_limit", {}).get("requests_per_minute", 15))
    limiter = SlidingWindowRateLimiter(requests_per_minute)
    snapshot_date = (
        datetime.fromisoformat(args.snapshot_date).date()
        if args.snapshot_date
        else datetime.fromisoformat(args.end_date).date()
    )

    dataset_name = "intraday_ohlcv_15m_60d" if args.mode == "base" else "intraday_ohlcv_15m_delta"
    table_name = "intraday_ohlcv_15m_base" if args.mode == "base" else "intraday_ohlcv_15m_delta"
    symbols = _resolve_symbols(args)

    start_time = datetime.now()
    started = time.perf_counter()
    steps_completed: list[str] = []
    steps_failed: list[str] = []

    total_fetched_rows = 0
    total_saved_rows = 0
    coverage: dict[str, dict[str, Any]] = {}

    try:
        logger.info(
            "Starting 15m intraday fetch",
            pipeline_name=PIPELINE_NAME,
            mode=args.mode,
            dataset_name=dataset_name,
            provider=DEFAULT_SOURCE_LABEL,
            interval=interval,
            symbols=symbols,
            end_date=args.end_date,
            snapshot_date=str(snapshot_date),
            max_workers=max_workers,
            requests_per_minute=requests_per_minute,
        )

        frames: list[pd.DataFrame] = []
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {}
            for symbol in symbols:
                symbol_start_date = _resolve_symbol_start(symbol, args)
                futures[
                    executor.submit(
                        _fetch_symbol,
                        symbol,
                        symbol_start_date,
                        args.end_date,
                        interval,
                        limiter,
                        logger,
                    )
                ] = (symbol, symbol_start_date)

            for future in as_completed(futures):
                symbol, symbol_start_date = futures[future]
                standardized_df = future.result()
                total_fetched_rows += len(standardized_df)
                if not standardized_df.empty:
                    standardized_df = standardized_df.copy()
                    standardized_df["snapshot_date"] = snapshot_date
                    coverage[symbol] = {
                        "rows": int(len(standardized_df)),
                        "requested_start": symbol_start_date,
                        "requested_end": args.end_date,
                        "actual_start": str(standardized_df["bar_time"].min()),
                        "actual_end": str(standardized_df["bar_time"].max()),
                    }
                    frames.append(standardized_df)
                else:
                    coverage[symbol] = {
                        "rows": 0,
                        "requested_start": symbol_start_date,
                        "requested_end": args.end_date,
                        "actual_start": None,
                        "actual_end": None,
                    }
        steps_completed.append("fetch_symbols")

        combined_df = (
            pd.concat(frames, ignore_index=True).sort_values(["symbol", "bar_time"]).reset_index(drop=True)
            if frames
            else pd.DataFrame()
        )
        steps_completed.append("standardize_intraday_15m")

        total_saved_rows = upsert_intraday_rows(table_name, combined_df)
        steps_completed.append("upsert_intraday_15m")

        materialized_files = materialize_dataset_from_table(
            dataset_name,
            symbols=symbols,
            snapshot_date=snapshot_date,
            max_workers=max_workers,
        )
        steps_completed.append("materialize_symbol_files")

        latest_ts = None
        if not combined_df.empty:
            latest_ts = pd.to_datetime(combined_df["bar_time"].max()).to_pydatetime()
        refresh_state = bump_refresh_version(dataset_name, run_id=run_id, latest_data_ts=latest_ts)
        steps_completed.append("bump_refresh_state")

        end_time = datetime.now()
        duration_seconds = time.perf_counter() - started
        record_pipeline_run(
            status="success",
            start_time=start_time,
            end_time=end_time,
            input_rows=total_fetched_rows,
            output_rows=total_saved_rows,
        )
        steps_completed.append("record_pipeline_run")

        logger.log_data_event(
            "ingestion",
            "Intraday 15m dataset refreshed",
            operation="intraday_15m_refresh",
            mode=args.mode,
            dataset_name=dataset_name,
            source=f"vnstock:{DEFAULT_SOURCE.lower()}",
            provider_label=DEFAULT_SOURCE_LABEL,
            symbols=symbols,
            end_date=args.end_date,
            snapshot_date=str(snapshot_date),
            rows_fetched=total_fetched_rows,
            rows_saved=total_saved_rows,
            coverage=coverage,
            materialized_files=materialized_files,
            refresh_state=refresh_state,
        )
        logger.log_pipeline_run(
            pipeline_name=PIPELINE_NAME,
            start_time=start_time.isoformat(),
            end_time=end_time.isoformat(),
            status="success",
            steps_completed=steps_completed,
            steps_failed=steps_failed,
            duration_seconds=duration_seconds,
            mode=args.mode,
            dataset_name=dataset_name,
            symbols=symbols,
            end_date=args.end_date,
            snapshot_date=str(snapshot_date),
            rows_fetched=total_fetched_rows,
            rows_saved=total_saved_rows,
            max_workers=max_workers,
            requests_per_minute=requests_per_minute,
            refresh_state=refresh_state,
        )
    except Exception as exc:
        end_time = datetime.now()
        duration_seconds = time.perf_counter() - started
        steps_failed.append("fetch_intraday_15m")
        logger.log_error(
            PIPELINE_NAME,
            type(exc).__name__,
            str(exc),
            context={
                "mode": args.mode,
                "dataset_name": dataset_name,
                "symbols": symbols,
                "end_date": args.end_date,
                "snapshot_date": str(snapshot_date),
                "max_workers": max_workers,
                "steps_completed": steps_completed,
                "steps_failed": steps_failed,
                "duration_seconds": duration_seconds,
                "rows_fetched": total_fetched_rows,
                "rows_saved": total_saved_rows,
            },
        )
        try:
            record_pipeline_run(
                status="failed",
                start_time=start_time,
                end_time=end_time,
                input_rows=total_fetched_rows,
                output_rows=total_saved_rows,
                error_message=str(exc),
            )
        except Exception as record_exc:
            logger.log_error(
                "record_pipeline_run",
                type(record_exc).__name__,
                str(record_exc),
                context={"original_error": str(exc)},
            )
        logger.log_pipeline_run(
            pipeline_name=PIPELINE_NAME,
            start_time=start_time.isoformat(),
            end_time=end_time.isoformat(),
            status="failed",
            steps_completed=steps_completed,
            steps_failed=steps_failed,
            error_message=str(exc),
            duration_seconds=duration_seconds,
            mode=args.mode,
            dataset_name=dataset_name,
            symbols=symbols,
            end_date=args.end_date,
            snapshot_date=str(snapshot_date),
            rows_fetched=total_fetched_rows,
            rows_saved=total_saved_rows,
            max_workers=max_workers,
        )
        raise


if __name__ == "__main__":
    main()
