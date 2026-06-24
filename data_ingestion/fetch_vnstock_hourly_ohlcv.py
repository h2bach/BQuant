"""Fetch hourly OHLCV data for VN30 using vnstock Quote history."""

from __future__ import annotations

import argparse
import contextlib
import io
import time
import uuid
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

import pandas as pd
import yaml

from data_ingestion.fetch_vnquant_ohlcv import get_vn30_symbols
from utils.logger import BQuantLogger
from warehouse.duckdb_connection import get_connection


REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_SOURCES_CONFIG_PATH = REPO_ROOT / "configs" / "data_sources.yaml"
DATASET_REGISTRY_PATH = REPO_ROOT / "configs" / "dataset_registry.yaml"
PIPELINE_NAME = "fetch_vnstock_hourly_ohlcv"
REQUEST_WINDOW_SECONDS = 60.0
REQUEST_TIMESTAMPS: deque[float] = deque()


def _load_yaml(path: Path) -> dict[str, Any]:
    """Load a YAML file into a dictionary."""
    with path.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def _resolve_repo_path(path_str: str) -> Path:
    """Resolve an absolute path or a repo-relative config path."""
    path = Path(path_str)
    if path.is_absolute():
        return path
    return (REPO_ROOT / path).resolve()


def _load_source_config() -> dict[str, Any]:
    """Return the configured vnstock source settings."""
    config = _load_yaml(DATA_SOURCES_CONFIG_PATH)
    return config.get("vnstock", {})


def _load_dataset_config(dataset_name: str) -> dict[str, Any]:
    """Load one dataset definition from the dataset registry."""
    registry = _load_yaml(DATASET_REGISTRY_PATH)
    datasets = registry.get("datasets", {})
    if dataset_name not in datasets:
        raise KeyError(f"Dataset {dataset_name} not found in {DATASET_REGISTRY_PATH}")
    return datasets[dataset_name]


def _import_quote_class() -> tuple[Any, str]:
    """Import `vnstock.api.quote.Quote` while capturing noisy startup output."""
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
        from vnstock.api.quote import Quote  # type: ignore

    return Quote, buffer.getvalue().strip()


def _call_with_suppressed_output(func: Callable[..., Any], *args: Any, **kwargs: Any) -> tuple[Any, str]:
    """Run a callable while capturing stdout/stderr for structured logging."""
    buffer = io.StringIO()
    try:
        with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
            result = func(*args, **kwargs)
    except BaseException as exc:
        captured_output = buffer.getvalue().strip()
        if captured_output:
            setattr(exc, "_bquant_suppressed_output", captured_output)
        raise
    return result, buffer.getvalue().strip()


def _default_start_date() -> str:
    """Return the default hourly backfill start timestamp."""
    return "2021-01-01 09:00:00"


def _default_end_date() -> str:
    """Return the current local timestamp as the default hourly end bound."""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _generate_yearly_windows(start_date: str, end_date: str) -> list[tuple[str, str]]:
    """Split a long hourly request into yearly windows for source stability."""
    start_dt = datetime.fromisoformat(start_date)
    end_dt = datetime.fromisoformat(end_date)

    windows: list[tuple[str, str]] = []
    current = start_dt
    while current <= end_dt:
        next_year = current.replace(year=current.year + 1)
        window_end = min(next_year - timedelta(seconds=1), end_dt)
        windows.append((current.strftime("%Y-%m-%d %H:%M:%S"), window_end.strftime("%Y-%m-%d %H:%M:%S")))
        current = window_end + timedelta(seconds=1)
    return windows


def _respect_rate_limit(requests_per_minute: int, logger: BQuantLogger, *, symbol: str, window_start: str, window_end: str) -> None:
    """Enforce a process-local sliding-window rate limit for vnstock requests."""
    effective_limit = max(int(requests_per_minute), 1)
    now = time.monotonic()
    while REQUEST_TIMESTAMPS and now - REQUEST_TIMESTAMPS[0] >= REQUEST_WINDOW_SECONDS:
        REQUEST_TIMESTAMPS.popleft()

    if len(REQUEST_TIMESTAMPS) < effective_limit:
        REQUEST_TIMESTAMPS.append(now)
        return

    wait_seconds = max(REQUEST_WINDOW_SECONDS - (now - REQUEST_TIMESTAMPS[0]) + 1.0, 1.0)
    logger.info(
        "Pausing to respect vnstock rate limit",
        pipeline_name=PIPELINE_NAME,
        symbol=symbol,
        window_start=window_start,
        window_end=window_end,
        requests_per_minute=effective_limit,
        wait_seconds=round(wait_seconds, 2),
    )
    time.sleep(wait_seconds)

    now = time.monotonic()
    while REQUEST_TIMESTAMPS and now - REQUEST_TIMESTAMPS[0] >= REQUEST_WINDOW_SECONDS:
        REQUEST_TIMESTAMPS.popleft()
    REQUEST_TIMESTAMPS.append(now)


def fetch_symbol_hourly_ohlcv(
    symbol: str,
    start_date: str,
    end_date: str,
    source_name: str,
    requests_per_minute: int,
    logger: BQuantLogger,
) -> pd.DataFrame:
    """Fetch and standardize hourly OHLCV chunks for one symbol from vnstock."""
    Quote, import_notice = _import_quote_class()
    if import_notice:
        logger.warning(
            "Suppressed vnstock import notices during adapter bootstrap",
            package="vnstock",
            notice_excerpt=import_notice[:500],
        )

    quote, quote_notice = _call_with_suppressed_output(
        Quote,
        symbol=symbol,
        source=source_name,
        show_log=False,
    )
    if quote_notice:
        logger.warning(
            "Suppressed vnstock runtime output during quote initialization",
            package="vnstock",
            symbol=symbol,
            source=source_name,
            notice_excerpt=quote_notice[:500],
        )

    # The source is fetched window-by-window so long lookbacks can recover partially
    # instead of failing as one large request.
    frames: list[pd.DataFrame] = []
    windows = _generate_yearly_windows(start_date, end_date)
    runtime_notice_logged = False

    for window_start, window_end in windows:
        try:
            _respect_rate_limit(
                requests_per_minute,
                logger,
                symbol=symbol,
                window_start=window_start,
                window_end=window_end,
            )
            chunk, runtime_notice = _call_with_suppressed_output(
                quote.history,
                start=window_start,
                end=window_end,
                interval="1H",
            )
            if runtime_notice and not runtime_notice_logged:
                logger.warning(
                    "Suppressed vnstock runtime output during hourly fetch",
                    package="vnstock",
                    symbol=symbol,
                    source=source_name,
                    notice_excerpt=runtime_notice[:500],
                )
                runtime_notice_logged = True
            if chunk is None or chunk.empty:
                logger.warning(
                    f"No hourly OHLCV returned for {symbol}",
                    operation="fetch_hourly_chunk",
                    symbol=symbol,
                    source=source_name,
                    window_start=window_start,
                    window_end=window_end,
                )
                continue
            frames.append(chunk.copy())
            logger.log_data_event(
                "ingestion",
                f"Fetched hourly OHLCV chunk for {symbol}",
                operation="fetch_hourly_chunk",
                symbol=symbol,
                source=source_name,
                window_start=window_start,
                window_end=window_end,
                row_count=len(chunk),
                actual_start=str(chunk["time"].min()),
                actual_end=str(chunk["time"].max()),
                )
        except BaseException as exc:
            if isinstance(exc, KeyboardInterrupt):
                raise
            suppressed_output = getattr(exc, "_bquant_suppressed_output", "")
            if suppressed_output and not runtime_notice_logged:
                logger.warning(
                    "Suppressed vnstock runtime output during failed hourly fetch",
                    package="vnstock",
                    symbol=symbol,
                    source=source_name,
                    notice_excerpt=str(suppressed_output)[:500],
                )
                runtime_notice_logged = True
            logger.warning(
                f"Hourly OHLCV chunk failed for {symbol}",
                operation="fetch_hourly_chunk",
                symbol=symbol,
                source=source_name,
                window_start=window_start,
                window_end=window_end,
                error_type=type(exc).__name__,
                error_message=str(exc),
            )

    if not frames:
        return pd.DataFrame(columns=["symbol", "bar_time", "open", "high", "low", "close", "volume", "interval", "source"])

    merged = pd.concat(frames, ignore_index=True)
    merged["symbol"] = symbol
    merged["bar_time"] = pd.to_datetime(merged["time"], errors="coerce")
    merged["interval"] = "1H"
    merged["source"] = f"vnstock:{source_name.lower()}"

    output = merged[["symbol", "bar_time", "open", "high", "low", "close", "volume", "interval", "source"]].copy()
    output = output.dropna(subset=["bar_time"])
    output = output.sort_values(["symbol", "bar_time"]).drop_duplicates(["symbol", "bar_time", "source"])
    for column in ["open", "high", "low", "close", "volume"]:
        output[column] = pd.to_numeric(output[column], errors="coerce")
    output = output.dropna(subset=["open", "high", "low", "close", "volume"])
    return output.reset_index(drop=True)


def _get_existing_hourly_keys(symbols: list[str], start_time: datetime, end_time: datetime) -> set[tuple[str, datetime, str]]:
    """Load existing raw hourly keys for duplicate suppression."""
    placeholders = ",".join(["?"] * len(symbols))
    sql = f"""
        SELECT symbol, bar_time, source
        FROM raw_ohlcv_hourly
        WHERE symbol IN ({placeholders})
          AND bar_time BETWEEN ? AND ?
    """
    params: list[Any] = [*symbols, start_time, end_time]
    with get_connection(read_only=True) as conn:
        rows = conn.execute(sql, params).fetchall()
    return {(str(symbol), pd.Timestamp(bar_time).to_pydatetime(), str(source)) for symbol, bar_time, source in rows}


def _get_latest_bar_time(symbol: str) -> datetime | None:
    """Return the latest saved raw hourly bar for one symbol."""
    with get_connection(read_only=True) as conn:
        value = conn.execute(
            """
            SELECT max(bar_time)
            FROM raw_ohlcv_hourly
            WHERE symbol = ?
            """,
            [symbol],
        ).fetchone()[0]
    if value is None:
        return None
    return pd.Timestamp(value).to_pydatetime()


def _filter_new_rows(df: pd.DataFrame) -> pd.DataFrame:
    """Keep only raw hourly rows that are not already stored in DuckDB."""
    if df.empty:
        return df.copy()
    symbols = sorted(df["symbol"].unique().tolist())
    start_time = pd.Timestamp(df["bar_time"].min()).to_pydatetime()
    end_time = pd.Timestamp(df["bar_time"].max()).to_pydatetime()
    existing_keys = _get_existing_hourly_keys(symbols, start_time, end_time)
    if not existing_keys:
        return df.copy()
    keys = [
        (
            str(row.symbol),
            pd.Timestamp(row.bar_time).to_pydatetime(),
            str(row.source),
        )
        for row in df.itertuples(index=False)
    ]
    mask = [key not in existing_keys for key in keys]
    return df.loc[mask].reset_index(drop=True)


def _write_partitioned_parquet(df: pd.DataFrame) -> list[str]:
    """Write raw hourly rows into symbol-partitioned Parquet files."""
    dataset_cfg = _load_dataset_config("raw_ohlcv_hourly")
    base_dir = _resolve_repo_path(dataset_cfg["parquet_path"])
    base_dir.mkdir(parents=True, exist_ok=True)

    written_files: list[str] = []
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    for symbol, symbol_df in df.groupby("symbol", sort=True):
        partition_dir = base_dir / f"symbol={symbol}"
        partition_dir.mkdir(parents=True, exist_ok=True)
        file_path = partition_dir / f"part-{timestamp}-{uuid.uuid4().hex[:8]}.parquet"
        symbol_df.to_parquet(file_path, index=False)
        written_files.append(str(file_path))
    return written_files


def save_raw_hourly_ohlcv(df: pd.DataFrame) -> tuple[int, list[str]]:
    """Persist only new raw hourly rows to DuckDB and partitioned Parquet."""
    if df.empty:
        return 0, []
    new_rows = _filter_new_rows(df)
    if new_rows.empty:
        return 0, []

    with get_connection(read_only=False) as conn:
        conn.register("tmp_raw_ohlcv_hourly", new_rows)
        conn.execute("INSERT INTO raw_ohlcv_hourly BY NAME SELECT * FROM tmp_raw_ohlcv_hourly")
        conn.unregister("tmp_raw_ohlcv_hourly")

    parquet_files = _write_partitioned_parquet(new_rows)
    return len(new_rows), parquet_files


def record_pipeline_run(
    *,
    status: str,
    start_time: datetime,
    end_time: datetime,
    input_rows: int,
    output_rows: int,
    error_message: str | None = None,
) -> None:
    """Persist one pipeline-run summary row into the shared pipeline registry."""
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


def parse_args() -> argparse.Namespace:
    """Parse CLI arguments for vnstock hourly raw ingestion."""
    parser = argparse.ArgumentParser(description="Fetch hourly OHLCV for VN30 using vnstock.")
    parser.add_argument("--symbols", nargs="*", help="Explicit symbols to fetch.")
    parser.add_argument("--test", action="store_true", help="Use test symbols from universe config.")
    parser.add_argument("--start-date", default=_default_start_date(), help="Start datetime in YYYY-MM-DD HH:MM:SS.")
    parser.add_argument("--end-date", default=_default_end_date(), help="End datetime in YYYY-MM-DD HH:MM:SS.")
    parser.add_argument("--incremental", action="store_true", help="Resume from the latest saved hourly bar per symbol.")
    parser.add_argument(
        "--lookback-days",
        type=int,
        default=5,
        help="Re-fetch this many days before the latest saved bar when using --incremental.",
    )
    return parser.parse_args()


def _resolve_symbols(args: argparse.Namespace) -> list[str]:
    """Resolve explicit or universe-config symbols for the current run."""
    if args.symbols:
        return [str(symbol).upper() for symbol in args.symbols]
    return get_vn30_symbols(use_test_symbols=args.test)


def _resolve_symbol_time_range(symbol: str, args: argparse.Namespace) -> tuple[str, str]:
    """Resolve a symbol-specific time range for full or incremental hourly fetches."""
    end_date = args.end_date
    if not args.incremental:
        return args.start_date, end_date

    latest_bar_time = _get_latest_bar_time(symbol)
    if latest_bar_time is None:
        return args.start_date, end_date

    incremental_start = latest_bar_time - timedelta(days=max(args.lookback_days, 0))
    end_dt = datetime.fromisoformat(end_date)
    if incremental_start > end_dt:
        incremental_start = end_dt - timedelta(days=max(args.lookback_days, 1))
    return incremental_start.strftime("%Y-%m-%d %H:%M:%S"), end_date


def main() -> None:
    """Fetch, deduplicate, and persist raw hourly OHLCV rows from vnstock."""
    args = parse_args()
    logger = BQuantLogger(PIPELINE_NAME)
    source_cfg = _load_source_config()
    provider = str(source_cfg.get("parameters", {}).get("hourly_source", "VCI")).upper()
    requests_per_minute = int(source_cfg.get("rate_limit", {}).get("requests_per_minute", 15))

    start_time = datetime.now()
    started = time.perf_counter()
    steps_completed: list[str] = []
    steps_failed: list[str] = []

    symbols = _resolve_symbols(args)
    total_fetched_rows = 0
    total_saved_rows = 0
    parquet_files: list[str] = []
    coverage: dict[str, dict[str, Any]] = {}

    try:
        logger.info(
            "Starting vnstock hourly OHLCV fetch",
            pipeline_name=PIPELINE_NAME,
            symbols=symbols,
            start_date=args.start_date,
            end_date=args.end_date,
            provider=provider,
            requests_per_minute=requests_per_minute,
            use_test_symbols=args.test,
            incremental=args.incremental,
            lookback_days=args.lookback_days,
        )

        for symbol in symbols:
            symbol_start_date, symbol_end_date = _resolve_symbol_time_range(symbol, args)
            symbol_df = fetch_symbol_hourly_ohlcv(
                symbol,
                symbol_start_date,
                symbol_end_date,
                provider,
                requests_per_minute,
                logger,
            )
            total_fetched_rows += len(symbol_df)
            if not symbol_df.empty:
                saved_rows, symbol_parquet_files = save_raw_hourly_ohlcv(symbol_df)
                total_saved_rows += saved_rows
                parquet_files.extend(symbol_parquet_files)
                coverage[symbol] = {
                    "rows": int(len(symbol_df)),
                    "saved_rows": int(saved_rows),
                    "deduped_rows": int(len(symbol_df) - saved_rows),
                    "requested_start": symbol_start_date,
                    "requested_end": symbol_end_date,
                    "actual_start": str(symbol_df["bar_time"].min()),
                    "actual_end": str(symbol_df["bar_time"].max()),
                }
                logger.log_data_event(
                    "ingestion",
                    f"Saved hourly OHLCV for {symbol}",
                    operation="save_raw_hourly_ohlcv",
                    symbol=symbol,
                    source=f"vnstock:{provider.lower()}",
                    rows_fetched=int(len(symbol_df)),
                    rows_saved=int(saved_rows),
                    deduped_rows=int(len(symbol_df) - saved_rows),
                    parquet_files=symbol_parquet_files,
                    requested_start=symbol_start_date,
                    requested_end=symbol_end_date,
                )
            else:
                coverage[symbol] = {
                    "rows": 0,
                    "saved_rows": 0,
                    "deduped_rows": 0,
                    "requested_start": symbol_start_date,
                    "requested_end": symbol_end_date,
                    "actual_start": None,
                    "actual_end": None,
                }
        steps_completed.append("fetch_symbols")
        steps_completed.append("standardize_hourly_ohlcv")
        steps_completed.append("save_raw_hourly_ohlcv")

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
            "Hourly OHLCV raw ingestion completed",
            operation="hourly_ohlcv_ingestion",
            source=f"vnstock:{provider.lower()}",
            symbols=symbols,
            start_date=args.start_date,
            end_date=args.end_date,
            requests_per_minute=requests_per_minute,
            rows_fetched=total_fetched_rows,
            rows_saved=total_saved_rows,
            parquet_files=parquet_files,
            deduped_rows=total_fetched_rows - total_saved_rows,
            coverage=coverage,
        )
        logger.log_pipeline_run(
            pipeline_name=PIPELINE_NAME,
            start_time=start_time.isoformat(),
            end_time=end_time.isoformat(),
            status="success",
            steps_completed=steps_completed,
            steps_failed=steps_failed,
            duration_seconds=duration_seconds,
            provider=provider,
            symbols=symbols,
            start_date=args.start_date,
            end_date=args.end_date,
            requests_per_minute=requests_per_minute,
            incremental=args.incremental,
            lookback_days=args.lookback_days,
            rows_fetched=total_fetched_rows,
            rows_saved=total_saved_rows,
            coverage=coverage,
        )
    except Exception as exc:
        end_time = datetime.now()
        duration_seconds = time.perf_counter() - started
        steps_failed.append("fetch_vnstock_hourly_ohlcv")
        logger.log_error(
            PIPELINE_NAME,
            type(exc).__name__,
            str(exc),
            context={
                "provider": provider,
                "requests_per_minute": requests_per_minute,
                "symbols": symbols,
                "start_date": args.start_date,
                "end_date": args.end_date,
                "incremental": args.incremental,
                "lookback_days": args.lookback_days,
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
            provider=provider,
            symbols=symbols,
            start_date=args.start_date,
            end_date=args.end_date,
            requests_per_minute=requests_per_minute,
            incremental=args.incremental,
            lookback_days=args.lookback_days,
            rows_fetched=total_fetched_rows,
            rows_saved=total_saved_rows,
        )
        raise


if __name__ == "__main__":
    main()
