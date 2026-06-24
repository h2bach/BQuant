"""Validate raw OHLCV datasets and materialize clean tables."""

from __future__ import annotations

import argparse
import shutil
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd
import yaml

from utils.logger import BQuantLogger
from warehouse.duckdb_connection import get_connection
from warehouse.parquet_io import export_table_to_parquet


REPO_ROOT = Path(__file__).resolve().parent.parent
DATASET_REGISTRY_PATH = REPO_ROOT / "configs" / "dataset_registry.yaml"
VALIDATION_RULES_PATH = REPO_ROOT / "configs" / "validation_rules.yaml"
PIPELINE_BASENAME = "validate_ohlcv"

SOURCE_PRIORITY = {
    "vnstock:vci": 0,
    "vnstock:tcbs": 1,
    "vnstock:msn": 2,
    "vnquant:vndirect": 10,
    "vnquant:cafef": 11,
}


@dataclass(frozen=True)
class DatasetSpec:
    """Describe the table/schema contract for one validation granularity."""
    granularity: str
    raw_dataset: str
    clean_dataset: str
    source_table: str
    target_table: str
    time_column: str
    time_kind: str
    raw_unique_columns: list[str]
    target_unique_columns: list[str]
    output_columns: list[str]


DATASET_SPECS = {
    "daily": DatasetSpec(
        granularity="daily",
        raw_dataset="raw_ohlcv",
        clean_dataset="clean_ohlcv_daily",
        source_table="raw_ohlcv",
        target_table="clean_ohlcv_daily",
        time_column="trading_date",
        time_kind="date",
        raw_unique_columns=["symbol", "trading_date", "source"],
        target_unique_columns=["symbol", "trading_date"],
        output_columns=[
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
    "hourly": DatasetSpec(
        granularity="hourly",
        raw_dataset="raw_ohlcv_hourly",
        clean_dataset="clean_ohlcv_hourly",
        source_table="raw_ohlcv_hourly",
        target_table="clean_ohlcv_hourly",
        time_column="bar_time",
        time_kind="timestamp",
        raw_unique_columns=["symbol", "bar_time", "source"],
        target_unique_columns=["symbol", "bar_time"],
        output_columns=[
            "symbol",
            "bar_time",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "interval",
            "source",
        ],
    ),
}


def _load_yaml(path: Path) -> dict[str, Any]:
    """Load a YAML file into a dictionary."""
    with path.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def _load_dataset_config(dataset_name: str) -> dict[str, Any]:
    """Load one dataset definition from the dataset registry."""
    registry = _load_yaml(DATASET_REGISTRY_PATH)
    datasets = registry.get("datasets", {})
    if dataset_name not in datasets:
        raise KeyError(f"Dataset {dataset_name} not found in {DATASET_REGISTRY_PATH}")
    return datasets[dataset_name]


def _resolve_output_path(dataset_name: str) -> Path:
    """Resolve the configured Parquet export path for a clean dataset."""
    config = _load_dataset_config(dataset_name)
    path = Path(config["parquet_path"])
    if path.is_absolute():
        return path
    return (REPO_ROOT / path).resolve()


def _load_validation_config(granularity: str) -> dict[str, Any]:
    """Load the applicable validation rules for the requested granularity."""
    config = _load_yaml(VALIDATION_RULES_PATH)
    if granularity == "hourly":
        return config.get("ohlcv_hourly_validation", {})
    return config.get("ohlcv_validation", {})


def parse_args() -> argparse.Namespace:
    """Parse CLI arguments for raw OHLCV validation."""
    parser = argparse.ArgumentParser(description="Validate raw OHLCV datasets and materialize clean tables.")
    parser.add_argument("--granularity", choices=sorted(DATASET_SPECS.keys()), default="hourly")
    parser.add_argument("--symbols", nargs="*", help="Optional symbol subset.")
    parser.add_argument("--start-date", help="Optional inclusive start bound.")
    parser.add_argument("--end-date", help="Optional inclusive end bound.")
    return parser.parse_args()


def _normalize_symbols(symbols: list[str] | None) -> list[str] | None:
    """Normalize optional symbol filters to uppercase exchange symbols."""
    if not symbols:
        return None
    return [str(symbol).upper() for symbol in symbols]


def _build_filters(spec: DatasetSpec, args: argparse.Namespace) -> tuple[str, list[Any]]:
    """Build SQL filter clauses for symbol and time-bound validation runs."""
    clauses: list[str] = []
    params: list[Any] = []

    symbols = _normalize_symbols(args.symbols)
    if symbols:
        placeholders = ",".join(["?"] * len(symbols))
        clauses.append(f"symbol IN ({placeholders})")
        params.extend(symbols)

    if args.start_date:
        operator = "CAST(? AS DATE)" if spec.time_kind == "date" else "CAST(? AS TIMESTAMP)"
        clauses.append(f"{spec.time_column} >= {operator}")
        params.append(args.start_date)

    if args.end_date:
        operator = "CAST(? AS DATE)" if spec.time_kind == "date" else "CAST(? AS TIMESTAMP)"
        clauses.append(f"{spec.time_column} <= {operator}")
        params.append(args.end_date)

    if not clauses:
        return "", params
    return "WHERE " + " AND ".join(clauses), params


def load_raw_dataset(spec: DatasetSpec, args: argparse.Namespace) -> pd.DataFrame:
    """Load the raw dataset slice to validate for the requested run scope."""
    select_columns = ", ".join(spec.output_columns + ["created_at"])
    where_clause, params = _build_filters(spec, args)
    sql = f"""
        SELECT {select_columns}
        FROM {spec.source_table}
        {where_clause}
        ORDER BY symbol, {spec.time_column}, created_at
    """
    with get_connection(read_only=True) as conn:
        return conn.execute(sql, params).df()


def _build_issue_counts(raw_df: pd.DataFrame, spec: DatasetSpec) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, int]]:
    """Split raw rows into clean/invalid sets and summarize rule-level issue counts."""
    if raw_df.empty:
        return raw_df.copy(), raw_df.copy(), {"raw_rows": 0, "invalid_rows": 0, "clean_rows": 0}

    frame = raw_df.copy()
    frame["_source_priority"] = frame["source"].map(lambda value: SOURCE_PRIORITY.get(str(value).lower(), 999))
    valid_mask = pd.Series(True, index=frame.index)
    issue_counts: dict[str, int] = {"raw_rows": int(len(frame))}

    symbol_missing = frame["symbol"].isna() | (frame["symbol"].astype(str).str.strip() == "")
    issue_counts["symbol_not_null"] = int(symbol_missing.sum())
    valid_mask &= ~symbol_missing

    time_values = pd.to_datetime(frame[spec.time_column], errors="coerce")
    frame[spec.time_column] = time_values
    min_time = pd.Timestamp("2000-01-01")
    max_time = pd.Timestamp(datetime.now())
    time_invalid = time_values.isna() | (time_values < min_time) | (time_values > max_time)
    issue_counts[f"{spec.time_column}_valid"] = int(time_invalid.sum())
    valid_mask &= ~time_invalid

    price_columns = ["open", "high", "low", "close"]
    for column in price_columns + ["volume"]:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    if "adjusted_close" in frame.columns:
        frame["adjusted_close"] = pd.to_numeric(frame["adjusted_close"], errors="coerce")

    prices_non_positive = (frame[price_columns] <= 0).any(axis=1) | frame[price_columns].isna().any(axis=1)
    issue_counts["ohlc_positive"] = int(prices_non_positive.sum())
    valid_mask &= ~prices_non_positive

    high_low_invalid = frame["high"] < frame["low"]
    issue_counts["high_ge_low"] = int(high_low_invalid.sum())
    valid_mask &= ~high_low_invalid

    range_invalid = (
        (frame["open"] > frame["high"])
        | (frame["close"] > frame["high"])
        | (frame["open"] < frame["low"])
        | (frame["close"] < frame["low"])
    )
    issue_counts["open_close_within_range"] = int(range_invalid.sum())
    valid_mask &= ~range_invalid

    volume_invalid = frame["volume"].isna() | (frame["volume"] < 0)
    issue_counts["volume_non_negative"] = int(volume_invalid.sum())
    valid_mask &= ~volume_invalid

    source_missing = frame["source"].isna() | (frame["source"].astype(str).str.strip() == "")
    issue_counts["source_not_null"] = int(source_missing.sum())
    valid_mask &= ~source_missing

    if "interval" in frame.columns:
        interval_missing = frame["interval"].isna() | (frame["interval"].astype(str).str.strip() == "")
        issue_counts["interval_not_null"] = int(interval_missing.sum())
        valid_mask &= ~interval_missing

    # Raw duplicate filtering is applied before source-priority dedupe so repeated
    # identical raw keys do not distort issue counts or downstream precedence rules.
    raw_duplicate_mask = frame.duplicated(spec.raw_unique_columns, keep="last")
    issue_counts["duplicate_raw_keys"] = int(raw_duplicate_mask.sum())
    valid_mask &= ~raw_duplicate_mask

    invalid_df = frame.loc[~valid_mask].copy()
    clean_df = frame.loc[valid_mask].copy()
    clean_df = clean_df.sort_values(
        spec.target_unique_columns + ["_source_priority", "created_at"],
        kind="stable",
    )
    target_duplicate_mask = clean_df.duplicated(spec.target_unique_columns, keep="first")
    issue_counts["duplicate_target_keys"] = int(target_duplicate_mask.sum())

    if int(target_duplicate_mask.sum()) > 0:
        invalid_df = pd.concat([invalid_df, clean_df.loc[target_duplicate_mask].copy()], ignore_index=True)
    clean_df = clean_df.loc[~target_duplicate_mask].copy()

    if spec.time_kind == "date":
        clean_df[spec.time_column] = pd.to_datetime(clean_df[spec.time_column], errors="coerce").dt.date
    else:
        clean_df[spec.time_column] = pd.to_datetime(clean_df[spec.time_column], errors="coerce")

    clean_df = clean_df.sort_values(spec.target_unique_columns, kind="stable").reset_index(drop=True)
    invalid_df = invalid_df.reset_index(drop=True)
    clean_df = clean_df[spec.output_columns]

    issue_counts["invalid_rows"] = int(len(invalid_df))
    issue_counts["clean_rows"] = int(len(clean_df))
    return clean_df, invalid_df, issue_counts


def _delete_target_slice(conn, spec: DatasetSpec, args: argparse.Namespace) -> None:
    """Delete the clean target slice that will be regenerated by this validation run."""
    where_clause, params = _build_filters(spec, args)
    if where_clause:
        conn.execute(f"DELETE FROM {spec.target_table} {where_clause}", params)
    else:
        conn.execute(f"DELETE FROM {spec.target_table}")


def write_clean_dataset(spec: DatasetSpec, clean_df: pd.DataFrame, args: argparse.Namespace) -> int:
    """Rewrite the clean target slice and return the table's total row count."""
    with get_connection(read_only=False) as conn:
        _delete_target_slice(conn, spec, args)
        if not clean_df.empty:
            conn.register("tmp_clean_ohlcv", clean_df)
            columns = ", ".join(spec.output_columns)
            conn.execute(
                f"INSERT INTO {spec.target_table} ({columns}) SELECT {columns} FROM tmp_clean_ohlcv"
            )
            conn.unregister("tmp_clean_ohlcv")
        total_rows = conn.execute(f"SELECT COUNT(*) FROM {spec.target_table}").fetchone()[0]
    return int(total_rows)


def export_clean_dataset(spec: DatasetSpec) -> str:
    """Export the clean target table to its configured Parquet location."""
    output_path = _resolve_output_path(spec.clean_dataset)
    shutil.rmtree(output_path, ignore_errors=True)
    output_path.mkdir(parents=True, exist_ok=True)
    export_table_to_parquet(spec.target_table, str(output_path))
    return str(output_path)


def record_pipeline_run(
    *,
    pipeline_name: str,
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
                pipeline_name,
                start_time,
                end_time,
                status,
                input_rows,
                output_rows,
                error_message,
            ],
        )


def main() -> None:
    """Validate raw OHLCV rows, rewrite the clean table, and export clean Parquet."""
    args = parse_args()
    spec = DATASET_SPECS[args.granularity]
    pipeline_name = f"{PIPELINE_BASENAME}_{args.granularity}"
    logger = BQuantLogger(pipeline_name)
    validation_cfg = _load_validation_config(args.granularity)

    start_time = datetime.now()
    started = time.perf_counter()
    steps_completed: list[str] = []
    steps_failed: list[str] = []

    try:
        logger.info(
            "Starting OHLCV validation",
            pipeline_name=pipeline_name,
            granularity=args.granularity,
            symbols=_normalize_symbols(args.symbols),
            start_date=args.start_date,
            end_date=args.end_date,
            rules=list(validation_cfg.keys()),
        )

        raw_df = load_raw_dataset(spec, args)
        steps_completed.append("load_raw_dataset")
        logger.log_data_event(
            "validation",
            "Loaded raw OHLCV dataset for validation",
            operation="load_raw_ohlcv",
            pipeline_name=pipeline_name,
            granularity=args.granularity,
            raw_rows=int(len(raw_df)),
            source_table=spec.source_table,
            target_table=spec.target_table,
        )

        clean_df, invalid_df, issue_counts = _build_issue_counts(raw_df, spec)
        steps_completed.append("validate_raw_dataset")
        if not invalid_df.empty:
            logger.warning(
                f"Validation removed {len(invalid_df)} rows from {spec.source_table}",
                pipeline_name=pipeline_name,
                granularity=args.granularity,
                issue_counts=issue_counts,
            )

        target_total_rows = write_clean_dataset(spec, clean_df, args)
        steps_completed.append("write_clean_dataset")

        exported_path = export_clean_dataset(spec)
        steps_completed.append("export_clean_dataset")

        end_time = datetime.now()
        duration_seconds = time.perf_counter() - started
        record_pipeline_run(
            pipeline_name=pipeline_name,
            status="success",
            start_time=start_time,
            end_time=end_time,
            input_rows=len(raw_df),
            output_rows=len(clean_df),
        )
        steps_completed.append("record_pipeline_run")

        logger.log_data_event(
            "validation",
            "OHLCV validation completed",
            operation="validate_ohlcv",
            pipeline_name=pipeline_name,
            granularity=args.granularity,
            raw_rows=int(len(raw_df)),
            invalid_rows=int(len(invalid_df)),
            clean_rows=int(len(clean_df)),
            target_total_rows=target_total_rows,
            issue_counts=issue_counts,
            output_path=exported_path,
        )
        logger.log_pipeline_run(
            pipeline_name=pipeline_name,
            start_time=start_time.isoformat(),
            end_time=end_time.isoformat(),
            status="success",
            steps_completed=steps_completed,
            steps_failed=steps_failed,
            duration_seconds=duration_seconds,
            granularity=args.granularity,
            raw_rows=int(len(raw_df)),
            invalid_rows=int(len(invalid_df)),
            clean_rows=int(len(clean_df)),
            target_total_rows=target_total_rows,
        )
    except Exception as exc:
        end_time = datetime.now()
        duration_seconds = time.perf_counter() - started
        steps_failed.append("validate_ohlcv")
        logger.log_error(
            pipeline_name,
            type(exc).__name__,
            str(exc),
            context={
                "granularity": args.granularity,
                "symbols": _normalize_symbols(args.symbols),
                "start_date": args.start_date,
                "end_date": args.end_date,
                "steps_completed": steps_completed,
                "steps_failed": steps_failed,
                "duration_seconds": duration_seconds,
            },
        )
        try:
            record_pipeline_run(
                pipeline_name=pipeline_name,
                status="failed",
                start_time=start_time,
                end_time=end_time,
                input_rows=0,
                output_rows=0,
                error_message=str(exc),
            )
        except Exception as record_exc:
            logger.log_error(
                "record_pipeline_run",
                type(record_exc).__name__,
                str(record_exc),
                context={"original_error": str(exc), "pipeline_name": pipeline_name},
            )
        logger.log_pipeline_run(
            pipeline_name=pipeline_name,
            start_time=start_time.isoformat(),
            end_time=end_time.isoformat(),
            status="failed",
            steps_completed=steps_completed,
            steps_failed=steps_failed,
            error_message=str(exc),
            duration_seconds=duration_seconds,
            granularity=args.granularity,
        )
        raise


if __name__ == "__main__":
    main()
