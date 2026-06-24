"""Connection helpers for the BQuant observability database."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import duckdb
import yaml


REPO_ROOT = Path(__file__).resolve().parent.parent
OBSERVABILITY_CONFIG_PATH = REPO_ROOT / "configs" / "observability.yaml"
OBSERVABILITY_SCHEMA_PATH = REPO_ROOT / "warehouse" / "observability_schema.sql"


def _load_observability_config() -> dict[str, Any]:
    if not OBSERVABILITY_CONFIG_PATH.exists():
        return {"database": {"path": "warehouse/bquant_observability.duckdb"}}
    with OBSERVABILITY_CONFIG_PATH.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def get_observability_db_path() -> str:
    config = _load_observability_config()
    path = config.get("database", {}).get("path", "warehouse/bquant_observability.duckdb")
    return str((REPO_ROOT / path).resolve())


def get_observability_connection(read_only: bool = False) -> duckdb.DuckDBPyConnection:
    config = _load_observability_config()
    database_cfg = config.get("database", {})
    db_path = Path(get_observability_db_path())
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = duckdb.connect(str(db_path), read_only=read_only)

    threads = database_cfg.get("threads")
    if threads:
        conn.execute(f"SET threads = {int(threads)};")

    memory_limit = database_cfg.get("memory_limit")
    if memory_limit:
        conn.execute("SET memory_limit = ?;", [str(memory_limit)])

    return conn


def execute_observability_sql_file(sql_path: str | None = None) -> None:
    schema_path = Path(sql_path) if sql_path else OBSERVABILITY_SCHEMA_PATH
    if not schema_path.is_absolute():
        schema_path = (REPO_ROOT / schema_path).resolve()
    with schema_path.open("r", encoding="utf-8") as handle:
        sql = handle.read()
    with get_observability_connection(read_only=False) as conn:
        conn.execute(sql)


def ensure_observability_initialized() -> str:
    execute_observability_sql_file()
    return get_observability_db_path()
