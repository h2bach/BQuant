"""DuckDB connection and SQL helpers for BQuant."""

from __future__ import annotations

import time
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


def _duckdb_config_options(duckdb_cfg: dict[str, Any]) -> dict[str, str]:
    """Build connection-time DuckDB configuration options.

    Args:
        duckdb_cfg: `duckdb` block from `configs/storage.yaml`.

    Returns:
        Mapping passed directly to `duckdb.connect(config=...)`. Applying these
        values at connect time keeps app, worker, and dbt connections compatible
        when they touch the same embedded database file.
    """
    options: dict[str, str] = {}
    if duckdb_cfg.get("threads"):
        options["threads"] = str(int(duckdb_cfg["threads"]))
    if duckdb_cfg.get("memory_limit"):
        options["memory_limit"] = str(duckdb_cfg["memory_limit"])
    return options


def _is_retryable_duckdb_error(exc: Exception) -> bool:
    """Return whether a DuckDB connection error is likely transient.

    Args:
        exc: Exception raised while opening the embedded DuckDB database.

    Returns:
        True for lock/configuration races between short-lived app, worker, and
        dbt connections; false for unrelated failures.
    """
    message = str(exc).lower()
    retryable_markers = [
        "conflicting lock",
        "different configuration",
        "can't open a connection",
        "could not set lock",
        "database file is locked",
    ]
    return any(marker in message for marker in retryable_markers)


def _connect_with_retry(
    db_path: Path,
    *,
    read_only: bool,
    config_options: dict[str, str],
    attempts: int = 6,
    sleep_seconds: float = 0.5,
) -> duckdb.DuckDBPyConnection:
    """Open a DuckDB connection with short backoff for transient locks.

    Args:
        db_path: Absolute DuckDB file path.
        read_only: Whether to open the connection in read-only mode.
        config_options: Connection-time DuckDB options.
        attempts: Maximum connection attempts.
        sleep_seconds: Initial sleep between retries.

    Returns:
        Open DuckDB connection.

    Raises:
        Exception: Re-raises the final DuckDB error if all retries fail.
    """
    last_exc: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return duckdb.connect(
                str(db_path),
                read_only=read_only,
                config=config_options or None,
            )
        except Exception as exc:
            last_exc = exc
            if attempt >= attempts or not _is_retryable_duckdb_error(exc):
                raise
            time.sleep(sleep_seconds * attempt)
    raise RuntimeError(f"Unable to connect to DuckDB at {db_path}") from last_exc


def get_connection(read_only: bool = False) -> duckdb.DuckDBPyConnection:
    config = _load_storage_config()
    duckdb_cfg = config.get("duckdb", {})
    db_path = Path(get_duckdb_path())
    db_path.parent.mkdir(parents=True, exist_ok=True)

    return _connect_with_retry(
        db_path,
        read_only=read_only,
        config_options=_duckdb_config_options(duckdb_cfg),
    )


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
