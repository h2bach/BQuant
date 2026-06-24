"""Fetch historical OHLCV data using vnquant-compatible source logic."""

from __future__ import annotations

import argparse
import json
import math
import re
import time
import uuid
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pandas as pd
import requests
import yaml

from utils.logger import BQuantLogger
from warehouse.duckdb_connection import get_connection


REPO_ROOT = Path(__file__).resolve().parent.parent
UNIVERSE_CONFIG_PATH = REPO_ROOT / "configs" / "universe_vn30.yaml"
DATA_SOURCES_CONFIG_PATH = REPO_ROOT / "configs" / "data_sources.yaml"
DATASET_REGISTRY_PATH = REPO_ROOT / "configs" / "dataset_registry.yaml"
PIPELINE_NAME = "fetch_vnquant_ohlcv"

API_VNDIRECT = "https://finfo-api.vndirect.com.vn/v4/stock_prices/"
URL_CAFEF = "https://cafef.vn/du-lieu/ajax/pagenew/datahistory/pricehistory.ashx"
HEADERS = {"content-type": "application/x-www-form-urlencoded", "User-Agent": "Mozilla"}
CHANGE_PATTERN = re.compile(r"([-+]?\d*[,\.]?\d+)\s*\(\s*([-+]?\d*[,\.]?\d+)\s*%\s*\)")
RUNTIME_BACKEND_STATUS = {"vndirect": True, "cafef": True}


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
    """Return the configured vnquant-compatible source settings."""
    config = _load_yaml(DATA_SOURCES_CONFIG_PATH)
    return config.get("vnquant", {})


def _load_dataset_config(dataset_name: str) -> dict[str, Any]:
    """Load one dataset definition from the dataset registry."""
    registry = _load_yaml(DATASET_REGISTRY_PATH)
    datasets = registry.get("datasets", {})
    if dataset_name not in datasets:
        raise KeyError(f"Dataset {dataset_name} not found in {DATASET_REGISTRY_PATH}")
    return datasets[dataset_name]


def _today_str() -> str:
    """Return today's date in ISO format."""
    return date.today().isoformat()


def _default_start_date() -> str:
    """Return the first day of the current year as the default backfill start."""
    return date(date.today().year, 1, 1).isoformat()


def _safe_import_vnquant() -> tuple[bool, str | None]:
    """Probe whether the optional `vnquant` package is importable in this environment."""
    try:
        import vnquant  # noqa: F401

        return True, None
    except Exception as exc:  # pragma: no cover - diagnostic path
        return False, f"{type(exc).__name__}: {exc}"


def _parse_number(value: Any) -> float:
    """Parse a backend numeric field into a float with empty values mapped to zero."""
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", "")
    if not text:
        return 0.0
    return float(text)


def _parse_change_str(value: Any) -> tuple[float | None, float | None]:
    """Parse a textual change string into absolute and percentage change values."""
    if value is None:
        return None, None
    match = CHANGE_PATTERN.search(str(value).replace(",", "."))
    if not match:
        return None, None
    return float(match.group(1)), float(match.group(2))


def get_vn30_symbols(use_test_symbols: bool = False) -> list[str]:
    """Load VN30 or test symbols from the universe config."""
    payload = _load_yaml(UNIVERSE_CONFIG_PATH)
    key = "test_symbols" if use_test_symbols else "symbols"
    symbols = payload.get(key, [])
    if not symbols:
        raise ValueError(f"No symbols found in {UNIVERSE_CONFIG_PATH} for key={key}")
    return [str(symbol).upper() for symbol in symbols]


def _fetch_vndirect(symbol: str, start_date: str, end_date: str, timeout_seconds: int) -> pd.DataFrame:
    """Fetch raw daily OHLCV rows from the VNDIRECT endpoint."""
    query = f"code:{symbol}~date:gte:{start_date}~date:lte:{end_date}"
    params = {
        "sort": "date",
        "size": max((datetime.fromisoformat(end_date) - datetime.fromisoformat(start_date)).days + 5, 30),
        "page": 1,
        "q": query,
    }
    response = requests.get(API_VNDIRECT, params=params, headers=HEADERS, timeout=timeout_seconds)
    response.raise_for_status()
    payload = response.json()
    data = payload.get("data", [])
    return pd.DataFrame(data)


def _fetch_cafef(symbol: str, start_date: str, end_date: str, timeout_seconds: int) -> pd.DataFrame:
    """Fetch raw daily OHLCV rows from the CAFEF endpoint."""
    params = {
        "Symbol": symbol,
        "StartDate": start_date,
        "EndDate": end_date,
        "PageIndex": 1,
        "PageSize": max((datetime.fromisoformat(end_date) - datetime.fromisoformat(start_date)).days + 5, 30),
    }
    response = requests.get(URL_CAFEF, params=params, timeout=timeout_seconds)
    response.raise_for_status()
    payload = response.json()
    data = ((payload.get("Data") or {}).get("Data")) or []
    return pd.DataFrame(data)


def fetch_ohlcv_vnquant(symbol: str, start_date: str, end_date: str, logger: BQuantLogger | None = None) -> tuple[pd.DataFrame, str]:
    """
    Fetch OHLCV data using vnquant-compatible source logic.

    Tries the VNDIRECT path first because it matches vnquant source code.
    Falls back to CAFEF if VNDIRECT is unavailable from the current environment.
    """
    source_cfg = _load_source_config()
    rate_limit = source_cfg.get("rate_limit", {})
    retry_attempts = int(rate_limit.get("retry_attempts", 3))
    retry_delay_seconds = int(rate_limit.get("retry_delay_seconds", 5))
    timeout_seconds = 15

    backends = [
        ("vndirect", _fetch_vndirect),
        ("cafef", _fetch_cafef),
    ]
    backend_errors: list[dict[str, Any]] = []

    for backend_name, backend_fn in backends:
        if not RUNTIME_BACKEND_STATUS.get(backend_name, True):
            continue
        for attempt in range(1, retry_attempts + 1):
            try:
                df = backend_fn(symbol, start_date, end_date, timeout_seconds)
                if logger:
                    logger.log_data_event(
                        "ingestion",
                        f"Fetched raw OHLCV for {symbol} via {backend_name}",
                        operation="fetch_raw_ohlcv",
                        symbol=symbol,
                        backend=backend_name,
                        attempt=attempt,
                        row_count=len(df),
                        start_date=start_date,
                        end_date=end_date,
                    )
                return df, backend_name
            except Exception as exc:
                backend_errors.append(
                    {
                        "backend": backend_name,
                        "attempt": attempt,
                        "error_type": type(exc).__name__,
                        "error_message": str(exc),
                    }
                )
                if isinstance(exc, (requests.exceptions.Timeout, requests.exceptions.ConnectionError)):
                    RUNTIME_BACKEND_STATUS[backend_name] = False
                if logger:
                    logger.warning(
                        f"Fetch attempt failed for {symbol} via {backend_name}",
                        operation="fetch_raw_ohlcv",
                        symbol=symbol,
                        backend=backend_name,
                        attempt=attempt,
                        error_type=type(exc).__name__,
                        error_message=str(exc),
                    )
                if not RUNTIME_BACKEND_STATUS.get(backend_name, True):
                    break
                if attempt < retry_attempts:
                    time.sleep(retry_delay_seconds)

    raise RuntimeError(
        f"All OHLCV fetch attempts failed for {symbol}: {json.dumps(backend_errors, ensure_ascii=True)}"
    )


def standardize_ohlcv(df: pd.DataFrame, symbol: str, source: str = "vnquant", backend: str | None = None) -> pd.DataFrame:
    """Standardize raw backend output to the project OHLCV schema."""
    if df.empty:
        return pd.DataFrame(
            columns=[
                "symbol",
                "trading_date",
                "open",
                "high",
                "low",
                "close",
                "adjusted_close",
                "volume",
                "source",
            ]
        )

    frame = df.copy()

    if "Ngay" in frame.columns:
        frame["trading_date"] = pd.to_datetime(frame["Ngay"], format="%d/%m/%Y", errors="coerce").dt.date
        frame["open"] = frame["GiaMoCua"].map(_parse_number)
        frame["high"] = frame["GiaCaoNhat"].map(_parse_number)
        frame["low"] = frame["GiaThapNhat"].map(_parse_number)
        frame["close"] = frame["GiaDongCua"].map(_parse_number)
        frame["adjusted_close"] = frame["GiaDieuChinh"].map(_parse_number)
        frame["volume"] = frame["KhoiLuongKhopLenh"].map(_parse_number) + frame["KLThoaThuan"].map(_parse_number)
    else:
        frame["trading_date"] = pd.to_datetime(frame["date"], errors="coerce").dt.date
        frame["open"] = frame["open"].map(_parse_number)
        frame["high"] = frame["high"].map(_parse_number)
        frame["low"] = frame["low"].map(_parse_number)
        frame["close"] = frame["close"].map(_parse_number)
        adjusted_col = "adClose" if "adClose" in frame.columns else "adjust_close"
        frame["adjusted_close"] = frame[adjusted_col].map(_parse_number)
        frame["volume"] = frame["nmVolume"].map(_parse_number) + frame["ptVolume"].map(_parse_number)

    frame["symbol"] = str(symbol).upper()
    frame["source"] = source if backend is None else f"{source}:{backend}"

    output = frame[
        [
            "symbol",
            "trading_date",
            "open",
            "high",
            "low",
            "close",
            "adjusted_close",
            "volume",
            "source",
        ]
    ].copy()

    output = output.dropna(subset=["trading_date"])
    output = output.sort_values(["symbol", "trading_date"]).drop_duplicates(["symbol", "trading_date", "source"])
    output["volume"] = output["volume"].astype(float)
    return output.reset_index(drop=True)


def _get_existing_keys(symbols: list[str], start_date: str, end_date: str) -> set[tuple[str, date, str]]:
    """Load existing raw daily keys for duplicate suppression."""
    placeholders = ",".join(["?"] * len(symbols))
    sql = f"""
        SELECT symbol, trading_date, source
        FROM raw_ohlcv
        WHERE symbol IN ({placeholders})
          AND trading_date BETWEEN ? AND ?
    """
    params: list[Any] = [*symbols, start_date, end_date]
    with get_connection(read_only=True) as conn:
        rows = conn.execute(sql, params).fetchall()
    return {(str(symbol), trading_date, str(source)) for symbol, trading_date, source in rows}


def _filter_new_rows(df: pd.DataFrame) -> pd.DataFrame:
    """Keep only raw daily rows that are not already stored in DuckDB."""
    if df.empty:
        return df.copy()
    symbols = sorted(df["symbol"].unique().tolist())
    start_date = min(df["trading_date"]).isoformat()
    end_date = max(df["trading_date"]).isoformat()
    existing_keys = _get_existing_keys(symbols, start_date, end_date)
    if not existing_keys:
        return df.copy()
    keys = list(zip(df["symbol"], df["trading_date"], df["source"]))
    mask = [key not in existing_keys for key in keys]
    return df.loc[mask].reset_index(drop=True)


def _write_raw_partitioned_parquet(df: pd.DataFrame) -> list[str]:
    """Write raw daily rows into symbol-partitioned Parquet files."""
    dataset_cfg = _load_dataset_config("raw_ohlcv")
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


def save_raw_ohlcv(df: pd.DataFrame) -> tuple[int, list[str]]:
    """Persist only new raw rows to DuckDB and partitioned Parquet."""
    if df.empty:
        return 0, []

    new_rows = _filter_new_rows(df)
    if new_rows.empty:
        return 0, []

    working_df = new_rows.copy()
    with get_connection(read_only=False) as conn:
        conn.register("tmp_raw_ohlcv", working_df)
        conn.execute("INSERT INTO raw_ohlcv BY NAME SELECT * FROM tmp_raw_ohlcv")
        conn.unregister("tmp_raw_ohlcv")

    parquet_files = _write_raw_partitioned_parquet(working_df)
    return len(working_df), parquet_files


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
    """Parse CLI arguments for vnquant-compatible raw daily ingestion."""
    parser = argparse.ArgumentParser(description="Fetch OHLCV data using vnquant-compatible sources.")
    parser.add_argument("--symbols", nargs="*", help="Explicit symbols to fetch. Overrides universe config.")
    parser.add_argument("--test", action="store_true", help="Use test symbols from universe config.")
    parser.add_argument("--start-date", default=_default_start_date(), help="Inclusive start date in YYYY-MM-DD.")
    parser.add_argument("--end-date", default=_today_str(), help="Inclusive end date in YYYY-MM-DD.")
    return parser.parse_args()


def _resolve_symbols(args: argparse.Namespace) -> list[str]:
    """Resolve explicit or universe-config symbols for the current run."""
    if args.symbols:
        return [str(symbol).upper() for symbol in args.symbols]
    return get_vn30_symbols(use_test_symbols=args.test)


def main() -> None:
    """Fetch, standardize, deduplicate, and persist raw daily OHLCV rows."""
    args = parse_args()
    logger = BQuantLogger(PIPELINE_NAME)
    start_time = datetime.now()
    started = time.perf_counter()
    steps_completed: list[str] = []
    steps_failed: list[str] = []

    symbols = _resolve_symbols(args)
    total_fetched_rows = 0
    total_saved_rows = 0
    standardized_frames: list[pd.DataFrame] = []
    backends_used: dict[str, str] = {}
    parquet_files: list[str] = []

    try:
        available, import_error = _safe_import_vnquant()
        logger.info(
            "Starting vnquant OHLCV fetch",
            pipeline_name=PIPELINE_NAME,
            symbols=symbols,
            start_date=args.start_date,
            end_date=args.end_date,
            use_test_symbols=args.test,
        )
        if not available:
            logger.warning(
                "vnquant import is unavailable; using direct source-compatible HTTP adapter",
                package="vnquant",
                import_error=import_error,
            )

        # Source fallback happens inside `fetch_ohlcv_vnquant`, but each symbol still
        # flows through the same raw-standardize-save lifecycle for consistent lineage.
        for symbol in symbols:
            raw_df, backend = fetch_ohlcv_vnquant(symbol, args.start_date, args.end_date, logger=logger)
            total_fetched_rows += len(raw_df)
            backends_used[symbol] = backend
            standardized_frames.append(standardize_ohlcv(raw_df, symbol=symbol, source="vnquant", backend=backend))

        steps_completed.append("fetch_symbols")

        combined_df = pd.concat(standardized_frames, ignore_index=True) if standardized_frames else pd.DataFrame()
        combined_df = combined_df.sort_values(["symbol", "trading_date"]).reset_index(drop=True) if not combined_df.empty else combined_df
        steps_completed.append("standardize_ohlcv")

        saved_rows, parquet_files = save_raw_ohlcv(combined_df)
        total_saved_rows = saved_rows
        steps_completed.append("save_raw_ohlcv")

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
            "OHLCV raw ingestion completed",
            operation="ohlcv_ingestion",
            source="vnquant",
            symbols=symbols,
            start_date=args.start_date,
            end_date=args.end_date,
            backends_used=backends_used,
            rows_fetched=total_fetched_rows,
            rows_saved=total_saved_rows,
            parquet_files=parquet_files,
            deduped_rows=total_fetched_rows - total_saved_rows,
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
            backends_used=backends_used,
        )
    except Exception as exc:
        end_time = datetime.now()
        duration_seconds = time.perf_counter() - started
        steps_failed.append("fetch_vnquant_ohlcv")
        logger.log_error(
            PIPELINE_NAME,
            type(exc).__name__,
            str(exc),
            context={
                "symbols": symbols,
                "start_date": args.start_date,
                "end_date": args.end_date,
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
            start_date=args.start_date,
            end_date=args.end_date,
            rows_fetched=total_fetched_rows,
            rows_saved=total_saved_rows,
        )
        raise


if __name__ == "__main__":
    main()
