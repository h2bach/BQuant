"""Fetch and materialize the canonical 10-year daily OHLCV base dataset."""

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

from data_ingestion.fetch_vn30_universe import DEFAULT_CONFIG_PATH, _load_yaml
from data_ingestion.yfinance_adapter import fetch_daily_history, source_config
from utils.logger import BQuantLogger
from utils.rate_limit import SlidingWindowRateLimiter
from warehouse.data_manifest import materialize_dataset_from_table
from warehouse.duckdb_connection import get_connection


REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_SOURCES_CONFIG_PATH = REPO_ROOT / "configs" / "data_sources.yaml"
DATA_MANAGEMENT_PATH = REPO_ROOT / "configs" / "data_management.yaml"
PIPELINE_NAME = "fetch_daily_10y_base"


def _load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def _source_config() -> dict[str, Any]:
    return source_config()


def _management_config() -> dict[str, Any]:
    return _load_yaml(DATA_MANAGEMENT_PATH).get("datasets", {}).get("daily_ohlcv_10y", {})


def _default_start_date() -> str:
    lookback_years = int(_source_config().get("parameters", {}).get("lookback_years", 10))
    start = date.today() - timedelta(days=lookback_years * 365 + 3)
    return start.isoformat()


def _default_end_date() -> str:
    return date.today().isoformat()


def _resolve_symbols(args: argparse.Namespace) -> list[str]:
    if args.symbols:
        return [str(symbol).upper() for symbol in args.symbols]
    payload = _load_yaml(DEFAULT_CONFIG_PATH)
    key = "test_symbols" if args.test else "symbols"
    symbols = payload.get(key, [])
    if not symbols:
        raise ValueError(f"No symbols found in universe config for key={key}")
    return [str(symbol).upper() for symbol in symbols]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch the 10-year daily OHLCV base dataset.")
    parser.add_argument("--symbols", nargs="*", help="Explicit symbols to fetch.")
    parser.add_argument("--test", action="store_true", help="Use the test universe.")
    parser.add_argument("--start-date", default=_default_start_date(), help="Inclusive start date YYYY-MM-DD.")
    parser.add_argument("--end-date", default=_default_end_date(), help="Inclusive end date YYYY-MM-DD.")
    parser.add_argument("--refresh-latest-day", action="store_true", help="Only refresh the latest daily bar.")
    return parser.parse_args()


def _fetch_symbol(
    symbol: str,
    start_date: str,
    end_date: str,
    limiter: SlidingWindowRateLimiter,
    logger: BQuantLogger,
) -> tuple[str, pd.DataFrame, str]:
    try:
        waited = limiter.acquire()
        if waited > 0:
            logger.info(
                "Paused to respect daily source rate limit",
                pipeline_name=PIPELINE_NAME,
                symbol=symbol,
                wait_seconds=round(waited, 2),
            )
        standardized = fetch_daily_history(symbol, start_date, end_date)
        if not standardized.empty:
            logger.log_data_event(
                "ingestion",
                f"Fetched daily OHLCV for {symbol} via yfinance",
                operation="fetch_daily_symbol",
                symbol=symbol,
                source="yfinance",
                start_date=start_date,
                end_date=end_date,
                row_count=len(standardized),
                actual_start=str(standardized["trading_date"].min()),
                actual_end=str(standardized["trading_date"].max()),
            )
        return symbol, standardized, "yfinance"
    except Exception as exc:
        logger.warning(
            f"Daily base fetch failed for {symbol}",
            operation="fetch_daily_symbol",
            symbol=symbol,
            start_date=start_date,
            end_date=end_date,
            error_type=type(exc).__name__,
            error_message=str(exc),
        )
        return symbol, pd.DataFrame(), "error"


def _delete_existing_slice(conn, symbol: str, start_date: Any, end_date: Any) -> None:
    conn.execute(
        """
        DELETE FROM daily_ohlcv_base
        WHERE symbol = ?
          AND trading_date BETWEEN ? AND ?
        """,
        [symbol, start_date, end_date],
    )


def upsert_daily_rows(df: pd.DataFrame) -> int:
    if df.empty:
        return 0

    working_df = df.copy()
    with get_connection(read_only=False) as conn:
        for symbol, symbol_df in working_df.groupby("symbol", sort=True):
            start_date = symbol_df["trading_date"].min()
            end_date = symbol_df["trading_date"].max()
            _delete_existing_slice(conn, str(symbol), start_date, end_date)
        conn.register("tmp_daily_base", working_df)
        conn.execute(
            """
            INSERT INTO daily_ohlcv_base (
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
            FROM tmp_daily_base
            """
        )
        conn.unregister("tmp_daily_base")
    return int(len(working_df))


def record_pipeline_run(
    *,
    status: str,
    start_time: datetime,
    end_time: datetime,
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
    args = parse_args()
    source_cfg = _source_config()
    mgmt_cfg = _management_config()
    logger = BQuantLogger(PIPELINE_NAME)

    start_date = args.end_date if args.refresh_latest_day else args.start_date
    end_date = args.end_date
    symbols = _resolve_symbols(args)
    rate_limit = int(source_cfg.get("rate_limit", {}).get("requests_per_minute", 60))
    max_workers = max(int(mgmt_cfg.get("parallel_fetch_workers", 4)), 1)
    limiter = SlidingWindowRateLimiter(rate_limit)

    start_time = datetime.now()
    started = time.perf_counter()
    steps_completed: list[str] = []
    steps_failed: list[str] = []

    total_fetched_rows = 0
    total_saved_rows = 0
    backend_usage: dict[str, str] = {}

    try:
        logger.info(
            "Starting 10-year daily base fetch",
            pipeline_name=PIPELINE_NAME,
            symbols=symbols,
            start_date=start_date,
            end_date=end_date,
            refresh_latest_day=args.refresh_latest_day,
            max_workers=max_workers,
            requests_per_minute=rate_limit,
        )

        frames: list[pd.DataFrame] = []
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(_fetch_symbol, symbol, start_date, end_date, limiter, logger): symbol
                for symbol in symbols
            }
            for future in as_completed(futures):
                symbol, standardized_df, backend = future.result()
                backend_usage[symbol] = backend
                total_fetched_rows += len(standardized_df)
                if not standardized_df.empty:
                    frames.append(standardized_df)
        steps_completed.append("fetch_symbols")

        combined_df = (
            pd.concat(frames, ignore_index=True).sort_values(["symbol", "trading_date"]).reset_index(drop=True)
            if frames
            else pd.DataFrame()
        )
        steps_completed.append("standardize_ohlcv")

        total_saved_rows = upsert_daily_rows(combined_df)
        steps_completed.append("upsert_daily_ohlcv_base")

        materialized_files = materialize_dataset_from_table("daily_ohlcv_10y", symbols=symbols, max_workers=max_workers)
        steps_completed.append("materialize_symbol_files")

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
            "Daily 10-year base dataset refreshed",
            operation="daily_10y_base_refresh",
            source="yfinance",
            symbols=symbols,
            start_date=start_date,
            end_date=end_date,
            refresh_latest_day=args.refresh_latest_day,
            rows_fetched=total_fetched_rows,
            rows_saved=total_saved_rows,
            materialized_files=materialized_files,
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
            start_date=start_date,
            end_date=end_date,
            rows_fetched=total_fetched_rows,
            rows_saved=total_saved_rows,
            max_workers=max_workers,
            backends_used=backend_usage,
        )
    except Exception as exc:
        end_time = datetime.now()
        duration_seconds = time.perf_counter() - started
        steps_failed.append("fetch_daily_10y_base")
        logger.log_error(
            PIPELINE_NAME,
            type(exc).__name__,
            str(exc),
            context={
                "symbols": symbols,
                "start_date": start_date,
                "end_date": end_date,
                "refresh_latest_day": args.refresh_latest_day,
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
            symbols=symbols,
            start_date=start_date,
            end_date=end_date,
            rows_fetched=total_fetched_rows,
            rows_saved=total_saved_rows,
            max_workers=max_workers,
        )
        raise


if __name__ == "__main__":
    main()
