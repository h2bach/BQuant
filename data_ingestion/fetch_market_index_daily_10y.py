"""Fetch and materialize the canonical 10-year daily OHLCV market index dataset."""

from __future__ import annotations

import argparse
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
import yaml

from data_ingestion.vnstock_adapter import DEFAULT_SOURCE, DEFAULT_SOURCE_LABEL, fetch_daily_history
from utils.logger import BQuantLogger
from utils.rate_limit import SlidingWindowRateLimiter
from warehouse.data_manifest import materialize_dataset_from_table
from warehouse.duckdb_connection import get_connection
from warehouse.refresh_state import bump_refresh_version


REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_SOURCES_CONFIG_PATH = REPO_ROOT / "configs" / "data_sources.yaml"
DATA_MANAGEMENT_PATH = REPO_ROOT / "configs" / "data_management.yaml"
PIPELINE_NAME = "fetch_market_index_daily_10y"
DEFAULT_INDEX_SYMBOLS = ["VNINDEX", "VN30"]


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
    """Return the vnstock source configuration block.

    Returns:
        `configs/data_sources.yaml` block keyed by `vnstock`.
    """
    return _load_yaml(DATA_SOURCES_CONFIG_PATH).get("vnstock", {})


def _management_config() -> dict[str, Any]:
    """Return dataset-management settings for the market index dataset.

    Returns:
        `market_index_daily_10y` block from `configs/data_management.yaml`.
    """
    return _load_yaml(DATA_MANAGEMENT_PATH).get("datasets", {}).get("market_index_daily_10y", {})


def _default_start_date() -> str:
    """Resolve the default trailing start date.

    Returns:
        ISO date string derived from configured lookback years plus a small
        buffer for calendar/trading-day mismatch.
    """
    lookback_years = int(_management_config().get("lookback_years", 10))
    return (date.today() - timedelta(days=lookback_years * 365 + 3)).isoformat()


def _default_end_date() -> str:
    """Return today's date as the default inclusive end date.

    Returns:
        Current local date in ISO `YYYY-MM-DD` form.
    """
    return date.today().isoformat()


def parse_args() -> argparse.Namespace:
    """Parse CLI arguments for market index daily ingestion.

    Returns:
        Namespace with index symbols and inclusive date-window values.
    """
    parser = argparse.ArgumentParser(description="Fetch the 10-year daily market index OHLCV dataset.")
    parser.add_argument("--symbols", nargs="*", default=DEFAULT_INDEX_SYMBOLS, help="Index symbols to fetch.")
    parser.add_argument("--start-date", default=_default_start_date(), help="Inclusive start date YYYY-MM-DD.")
    parser.add_argument("--end-date", default=_default_end_date(), help="Inclusive end date YYYY-MM-DD.")
    return parser.parse_args()


def _fetch_index(
    symbol: str,
    start_date: str,
    end_date: str,
    source_name: str,
    limiter: SlidingWindowRateLimiter,
    logger: BQuantLogger,
) -> tuple[str, pd.DataFrame, str]:
    """Fetch one market index symbol from vnstock with rate limiting.

    Args:
        symbol: Market index symbol such as `VNINDEX` or `VN30`.
        start_date: Inclusive start date.
        end_date: Inclusive end date.
        source_name: vnstock API provider code, normally `VCI`; BQuant
            documents this provider as `VCI-data-source`.
        limiter: Shared rate limiter for provider requests.
        logger: Structured logger for ingestion events.

    Returns:
        Tuple of symbol, standardized OHLCV DataFrame, and source label. Failed
        fetches return an empty frame and source label `error`.

    Side Effects:
        May sleep for rate limiting and emits structured ingestion logs.
    """
    try:
        waited = limiter.acquire()
        if waited > 0:
            logger.info(
                "Paused to respect market index source rate limit",
                pipeline_name=PIPELINE_NAME,
                symbol=symbol,
                wait_seconds=round(waited, 2),
            )
        source_label = f"vnstock:{source_name.lower()}"
        frame = fetch_daily_history(symbol, start_date, end_date, source=source_name)
        if not frame.empty:
            logger.log_data_event(
                "ingestion",
                f"Fetched daily market index OHLCV for {symbol}",
                operation="fetch_market_index_daily_symbol",
                symbol=symbol,
                source=source_label,
                provider_label=DEFAULT_SOURCE_LABEL,
                start_date=start_date,
                end_date=end_date,
                row_count=len(frame),
                actual_start=str(frame["trading_date"].min()),
                actual_end=str(frame["trading_date"].max()),
            )
        return symbol, frame, source_label
    except BaseException as exc:
        if isinstance(exc, KeyboardInterrupt):
            raise
        logger.warning(
            f"Market index daily fetch failed for {symbol}",
            operation="fetch_market_index_daily_symbol",
            symbol=symbol,
            source=f"vnstock:{source_name.lower()}",
            provider_label=DEFAULT_SOURCE_LABEL,
            start_date=start_date,
            end_date=end_date,
            error_type=type(exc).__name__,
            error_message=str(exc),
        )
        return symbol, pd.DataFrame(), "error"


def _delete_existing_slice(conn, symbol: str, start_date: Any, end_date: Any) -> None:
    """Delete one index/date slice before inserting refreshed rows.

    Args:
        conn: Open writable DuckDB connection.
        symbol: Market index symbol whose rows should be replaced.
        start_date: Inclusive slice start date.
        end_date: Inclusive slice end date.

    Side Effects:
        Deletes matching rows from `market_index_daily_base`.
    """
    conn.execute(
        """
        DELETE FROM market_index_daily_base
        WHERE symbol = ?
          AND trading_date BETWEEN ? AND ?
        """,
        [symbol, start_date, end_date],
    )


def upsert_market_index_rows(df: pd.DataFrame) -> int:
    """Replace affected symbol/date slices in `market_index_daily_base`.

    Args:
        df: Standardized daily index OHLCV rows from `vnstock_adapter`.

    Returns:
        Number of rows inserted after slice deletion.

    Side Effects:
        Opens a writable DuckDB connection, deletes overlapping slices, and
        inserts refreshed rows.
    """
    if df.empty:
        return 0

    working_df = df.copy()
    with get_connection(read_only=False) as conn:
        for symbol, symbol_df in working_df.groupby("symbol", sort=True):
            _delete_existing_slice(conn, str(symbol), symbol_df["trading_date"].min(), symbol_df["trading_date"].max())
        conn.register("tmp_market_index_daily", working_df)
        conn.execute(
            """
            INSERT INTO market_index_daily_base (
                symbol,
                trading_date,
                open,
                high,
                low,
                close,
                adjusted_close,
                volume,
                source
            )
            SELECT
                symbol,
                trading_date,
                open,
                high,
                low,
                close,
                adjusted_close,
                volume,
                source
            FROM tmp_market_index_daily
            """
        )
        conn.unregister("tmp_market_index_daily")
    return int(len(working_df))


def record_pipeline_run(
    *,
    run_id: str,
    status: str,
    start_time: datetime,
    end_time: datetime,
    input_rows: int,
    output_rows: int,
    error_message: str | None = None,
) -> None:
    """Persist one pipeline-run summary row.

    Args:
        run_id: Unique pipeline run id.
        status: Final status such as `success` or `failed`.
        start_time: Job start timestamp.
        end_time: Job end timestamp.
        input_rows: Number of fetched input rows.
        output_rows: Number of rows written/materialized.
        error_message: Optional failure message.

    Side Effects:
        Upserts into `pipeline_runs`.
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
            ON CONFLICT (run_id) DO UPDATE SET
                end_time = excluded.end_time,
                status = excluded.status,
                input_rows = excluded.input_rows,
                output_rows = excluded.output_rows,
                error_message = excluded.error_message
            """,
            [run_id, PIPELINE_NAME, start_time, end_time, status, input_rows, output_rows, error_message],
        )


def main() -> None:
    """Fetch, upsert, materialize, and version the market index dataset.

    Side Effects:
        Fetches VNINDEX/VN30 data from the vnstock `VCI-data-source` provider, writes
        `market_index_daily_base`, materializes per-index parquet files, bumps
        refresh state, and writes pipeline logs.
    """
    args = parse_args()
    source_cfg = _source_config()
    mgmt_cfg = _management_config()
    logger = BQuantLogger(PIPELINE_NAME)
    run_id = str(uuid.uuid4())

    symbols = [str(symbol).upper() for symbol in args.symbols]
    source_name = str(source_cfg.get("parameters", {}).get("default_source", DEFAULT_SOURCE)).upper()
    rate_limit = int(source_cfg.get("rate_limit", {}).get("requests_per_minute", 60))
    max_workers = max(int(mgmt_cfg.get("parallel_fetch_workers", 2)), 1)
    limiter = SlidingWindowRateLimiter(rate_limit)

    start_time = datetime.now()
    started = time.perf_counter()
    steps_completed: list[str] = []
    steps_failed: list[str] = []
    total_fetched_rows = 0
    total_saved_rows = 0
    backend_usage: dict[str, str] = {}

    try:
        frames: list[pd.DataFrame] = []
        logger.info(
            "Starting 10-year market index daily fetch",
            pipeline_name=PIPELINE_NAME,
            symbols=symbols,
            start_date=args.start_date,
            end_date=args.end_date,
            source=f"vnstock:{source_name.lower()}",
            provider_label=DEFAULT_SOURCE_LABEL,
            max_workers=max_workers,
            requests_per_minute=rate_limit,
        )
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(_fetch_index, symbol, args.start_date, args.end_date, source_name, limiter, logger): symbol
                for symbol in symbols
            }
            for future in as_completed(futures):
                symbol, frame, backend = future.result()
                backend_usage[symbol] = backend
                total_fetched_rows += len(frame)
                if not frame.empty:
                    frames.append(frame)
        steps_completed.append("fetch_indices")

        combined_df = (
            pd.concat(frames, ignore_index=True).sort_values(["symbol", "trading_date"]).reset_index(drop=True)
            if frames
            else pd.DataFrame()
        )
        steps_completed.append("standardize_ohlcv")

        total_saved_rows = upsert_market_index_rows(combined_df)
        steps_completed.append("upsert_market_index_daily_base")

        materialized_files = materialize_dataset_from_table("market_index_daily_10y", symbols=symbols, max_workers=max_workers)
        steps_completed.append("materialize_index_files")

        latest_ts = None
        if not combined_df.empty:
            latest_ts = pd.to_datetime(combined_df["trading_date"].max()).to_pydatetime()
        refresh_state = bump_refresh_version("market_index_daily_10y", run_id=run_id, latest_data_ts=latest_ts)
        steps_completed.append("bump_refresh_state")

        end_time = datetime.now()
        duration_seconds = time.perf_counter() - started
        record_pipeline_run(
            run_id=run_id,
            status="success",
            start_time=start_time,
            end_time=end_time,
            input_rows=total_fetched_rows,
            output_rows=total_saved_rows,
        )
        steps_completed.append("record_pipeline_run")

        logger.log_data_event(
            "ingestion",
            "Market index daily 10-year dataset refreshed",
            operation="market_index_daily_10y_refresh",
            source=f"vnstock:{source_name.lower()}",
            provider_label=DEFAULT_SOURCE_LABEL,
            symbols=symbols,
            start_date=args.start_date,
            end_date=args.end_date,
            rows_fetched=total_fetched_rows,
            rows_saved=total_saved_rows,
            materialized_files=materialized_files,
            refresh_state=refresh_state,
            backends_used=backend_usage,
        )
        logger.log_pipeline_run(
            pipeline_name=PIPELINE_NAME,
            start_time=start_time.isoformat(),
            end_time=end_time.isoformat(),
            status="success",
            steps_completed=steps_completed,
            steps_failed=steps_failed,
            duration_seconds=duration_seconds,
            symbols=symbols,
            start_date=args.start_date,
            end_date=args.end_date,
            rows_fetched=total_fetched_rows,
            rows_saved=total_saved_rows,
            max_workers=max_workers,
            source=f"vnstock:{source_name.lower()}",
            provider_label=DEFAULT_SOURCE_LABEL,
            refresh_state=refresh_state,
            backends_used=backend_usage,
        )
    except Exception as exc:
        end_time = datetime.now()
        duration_seconds = time.perf_counter() - started
        steps_failed.append("fetch_market_index_daily_10y")
        logger.log_error(
            PIPELINE_NAME,
            type(exc).__name__,
            str(exc),
            context={
                "run_id": run_id,
                "symbols": symbols,
                "start_date": args.start_date,
                "end_date": args.end_date,
                "max_workers": max_workers,
                "steps_completed": steps_completed,
                "steps_failed": steps_failed,
                "duration_seconds": duration_seconds,
                "rows_fetched": total_fetched_rows,
                "rows_saved": total_saved_rows,
            },
        )
        record_pipeline_run(
            run_id=run_id,
            status="failed",
            start_time=start_time,
            end_time=end_time,
            input_rows=total_fetched_rows,
            output_rows=total_saved_rows,
            error_message=str(exc),
        )
        raise


if __name__ == "__main__":
    main()
