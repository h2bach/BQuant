"""DuckDB connection and SQL helpers for BQuant."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import duckdb
import yaml


REPO_ROOT = Path(__file__).resolve().parent.parent
STORAGE_CONFIG_PATH = REPO_ROOT / "configs" / "storage.yaml"


def _load_storage_config() -> dict[str, Any]:
    with STORAGE_CONFIG_PATH.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def get_duckdb_path() -> str:
    config = _load_storage_config()
    path = config.get("duckdb", {}).get("path", "warehouse/bquant.duckdb")
    return str((REPO_ROOT / path).resolve())


def get_connection(read_only: bool = False) -> duckdb.DuckDBPyConnection:
    config = _load_storage_config()
    duckdb_cfg = config.get("duckdb", {})
    db_path = Path(get_duckdb_path())
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = duckdb.connect(str(db_path), read_only=read_only)

    threads = duckdb_cfg.get("threads")
    if threads:
        conn.execute(f"SET threads = {int(threads)};")

    memory_limit = duckdb_cfg.get("memory_limit")
    if memory_limit:
        conn.execute("SET memory_limit = ?;", [str(memory_limit)])

    return conn


def execute_sql_file(sql_path: str) -> None:
    sql_file = Path(sql_path)
    if not sql_file.is_absolute():
        sql_file = (REPO_ROOT / sql_path).resolve()
    with sql_file.open("r", encoding="utf-8") as handle:
        sql = handle.read()
    with get_connection(read_only=False) as conn:
        conn.execute(sql)


def query_df(sql: str, params: list[Any] | None = None):
    with get_connection(read_only=True) as conn:
        if params:
            return conn.execute(sql, params).df()
        return conn.execute(sql).df()


def write_df(table_name: str, df, mode: str = "append") -> None:
    temp_name = f"tmp_{table_name}"
    with get_connection(read_only=False) as conn:
        conn.register(temp_name, df)
        table_exists = conn.execute(
            "SELECT COUNT(*) FROM information_schema.tables WHERE table_name = ?",
            [table_name],
        ).fetchone()[0]
        if mode == "replace":
            if table_exists:
                conn.execute(f"DELETE FROM {table_name}")
                conn.execute(f"INSERT INTO {table_name} BY NAME SELECT * FROM {temp_name}")
            else:
                conn.execute(f"CREATE TABLE {table_name} AS SELECT * FROM {temp_name}")
        elif mode == "append":
            if table_exists:
                conn.execute(f"INSERT INTO {table_name} BY NAME SELECT * FROM {temp_name}")
            else:
                conn.execute(f"CREATE TABLE {table_name} AS SELECT * FROM {temp_name}")
        else:
            raise ValueError(f"Unsupported write mode: {mode}")
        conn.unregister(temp_name)
