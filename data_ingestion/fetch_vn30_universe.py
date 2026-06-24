"""Load VN30 universe membership from config and persist it."""

from __future__ import annotations

import argparse
import shutil
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd
import yaml

from utils.logger import BQuantLogger
from warehouse.duckdb_connection import get_connection, write_df
from warehouse.parquet_io import export_table_to_parquet


REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = REPO_ROOT / "configs" / "universe_vn30.yaml"
DATASET_REGISTRY_PATH = REPO_ROOT / "configs" / "dataset_registry.yaml"
PIPELINE_NAME = "fetch_vn30_universe"


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


def _resolve_repo_path(path_str: str) -> Path:
    """Resolve an absolute path or a repo-relative config path."""
    path = Path(path_str)
    if path.is_absolute():
        return path
    return (REPO_ROOT / path).resolve()


def _clear_output_dir(directory: Path) -> None:
    """Remove all files and subdirectories from a dataset output directory."""
    if not directory.exists():
        return
    for child in directory.iterdir():
        if child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink()


def load_vn30_universe(config_path: str, use_test_symbols: bool = False) -> pd.DataFrame:
    """Load VN30 universe config and return standardized membership rows."""
    config_file = _resolve_repo_path(config_path)
    payload = _load_yaml(config_file)

    universe = payload.get("universe", {})
    symbols = payload.get("test_symbols" if use_test_symbols else "symbols", [])
    if not symbols:
        raise ValueError(f"No symbols found in {config_file}")

    effective_date = pd.to_datetime(universe.get("effective_date")).date()
    universe_name = universe.get("name", "VN30")
    source = universe.get("source", "manual_config")

    rows = [
        {
            "universe_name": universe_name,
            "symbol": symbol,
            "effective_date": effective_date,
            "end_date": pd.NaT,
            "source": source,
        }
        for symbol in symbols
    ]
    return pd.DataFrame(rows)


def save_universe_to_duckdb(df: pd.DataFrame) -> None:
    """Persist universe membership to DuckDB using config-driven table settings."""
    dataset_cfg = _load_dataset_config("universe_members")
    table_name = dataset_cfg["duckdb_table"]
    mode = dataset_cfg.get("mode", "replace")

    working_df = df.copy()
    working_df["effective_date"] = pd.to_datetime(working_df["effective_date"]).dt.date
    working_df["end_date"] = pd.to_datetime(working_df["end_date"], errors="coerce").dt.date
    working_df["source"] = working_df["source"].astype(str)
    working_df["universe_name"] = working_df["universe_name"].astype(str)
    working_df["symbol"] = working_df["symbol"].astype(str)

    write_df(table_name, working_df, mode=mode)


def save_universe_to_parquet() -> Path:
    """Export universe_members DuckDB table to the registered Parquet path."""
    dataset_cfg = _load_dataset_config("universe_members")
    output_dir = _resolve_repo_path(dataset_cfg["parquet_path"])
    if dataset_cfg.get("overwrite", False):
        _clear_output_dir(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    export_table_to_parquet(dataset_cfg["duckdb_table"], str(output_dir))
    return output_dir


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
    """Parse CLI arguments for VN30 universe ingestion."""
    parser = argparse.ArgumentParser(description="Load VN30 universe membership into DuckDB and Parquet.")
    parser.add_argument(
        "--config-path",
        default=str(DEFAULT_CONFIG_PATH),
        help="Path to universe config YAML.",
    )
    parser.add_argument(
        "--test",
        action="store_true",
        help="Load only the test_symbols subset from the config.",
    )
    return parser.parse_args()


def main() -> None:
    """Load, persist, and export the VN30 universe membership dataset."""
    args = parse_args()
    logger = BQuantLogger(PIPELINE_NAME)
    start_time = datetime.now()
    started = time.perf_counter()
    steps_completed: list[str] = []
    steps_failed: list[str] = []

    try:
        logger.info(
            "Starting VN30 universe load",
            pipeline_name=PIPELINE_NAME,
            config_path=str(_resolve_repo_path(args.config_path)),
            use_test_symbols=args.test,
        )

        df = load_vn30_universe(args.config_path, use_test_symbols=args.test)
        steps_completed.append("load_config")

        save_universe_to_duckdb(df)
        steps_completed.append("write_duckdb")

        parquet_dir = save_universe_to_parquet()
        steps_completed.append("export_parquet")

        end_time = datetime.now()
        duration_seconds = time.perf_counter() - started
        record_pipeline_run(
            status="success",
            start_time=start_time,
            end_time=end_time,
            input_rows=len(df),
            output_rows=len(df),
        )
        steps_completed.append("record_pipeline_run")

        logger.log_data_event(
            "ingestion",
            "VN30 universe persisted successfully",
            operation="universe_ingestion",
            dataset="universe_members",
            universe_name=str(df["universe_name"].iloc[0]),
            symbols=df["symbol"].tolist(),
            row_count=len(df),
            use_test_symbols=args.test,
            parquet_path=str(parquet_dir),
        )
        logger.log_pipeline_run(
            pipeline_name=PIPELINE_NAME,
            start_time=start_time.isoformat(),
            end_time=end_time.isoformat(),
            status="success",
            steps_completed=steps_completed,
            steps_failed=steps_failed,
            duration_seconds=duration_seconds,
            row_count=len(df),
            use_test_symbols=args.test,
            parquet_path=str(parquet_dir),
        )
    except Exception as exc:
        end_time = datetime.now()
        duration_seconds = time.perf_counter() - started
        steps_failed.append("fetch_vn30_universe")
        logger.log_error(
            PIPELINE_NAME,
            type(exc).__name__,
            str(exc),
            context={
                "config_path": str(_resolve_repo_path(args.config_path)),
                "use_test_symbols": args.test,
                "steps_completed": steps_completed,
                "steps_failed": steps_failed,
                "duration_seconds": duration_seconds,
            },
        )
        try:
            record_pipeline_run(
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
            use_test_symbols=args.test,
        )
        raise


if __name__ == "__main__":
    main()
