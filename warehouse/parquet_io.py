"""Parquet helpers for BQuant datasets."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import duckdb
import yaml

from warehouse.duckdb_connection import get_connection


REPO_ROOT = Path(__file__).resolve().parent.parent
DATASET_REGISTRY_PATH = REPO_ROOT / "configs" / "dataset_registry.yaml"


def _load_registry() -> dict[str, Any]:
    with DATASET_REGISTRY_PATH.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def write_parquet(df, path: str, partition_cols: list[str] | None = None) -> None:
    output_path = Path(path)
    if not output_path.is_absolute():
        output_path = (REPO_ROOT / path).resolve()
    output_path.mkdir(parents=True, exist_ok=True)

    temp_name = "tmp_write_parquet"
    with get_connection(read_only=False) as conn:
        conn.register(temp_name, df)
        if partition_cols:
            cols = ", ".join(partition_cols)
            conn.execute(
                f"COPY {temp_name} TO ? (FORMAT PARQUET, PARTITION_BY ({cols}), OVERWRITE_OR_IGNORE 1)",
                [str(output_path)],
            )
        else:
            conn.execute(
                f"COPY {temp_name} TO ? (FORMAT PARQUET, OVERWRITE_OR_IGNORE 1)",
                [str(output_path / 'part-00000.parquet')],
            )
        conn.unregister(temp_name)


def read_parquet(path: str):
    parquet_path = Path(path)
    if not parquet_path.is_absolute():
        parquet_path = (REPO_ROOT / path).resolve()
    with duckdb.connect(":memory:") as conn:
        return conn.execute("SELECT * FROM read_parquet(?)", [str(parquet_path)]).df()


def export_table_to_parquet(table_name: str, output_path: str) -> None:
    registry = _load_registry()
    datasets = registry.get("datasets", {})
    partition_cols: list[str] | None = None
    for dataset in datasets.values():
        if dataset.get("duckdb_table") == table_name:
            partition_cols = dataset.get("partition_by") or None
            break

    output = Path(output_path)
    if not output.is_absolute():
        output = (REPO_ROOT / output_path).resolve()
    output.mkdir(parents=True, exist_ok=True)

    with get_connection(read_only=False) as conn:
        if partition_cols:
            cols = ", ".join(partition_cols)
            conn.execute(
                f"COPY {table_name} TO ? (FORMAT PARQUET, PARTITION_BY ({cols}), OVERWRITE_OR_IGNORE 1)",
                [str(output)],
            )
        else:
            conn.execute(
                f"COPY {table_name} TO ? (FORMAT PARQUET, OVERWRITE_OR_IGNORE 1)",
                [str(output / 'part-00000.parquet')],
            )
