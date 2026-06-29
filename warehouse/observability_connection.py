"""Connection helpers for the BQuant observability database."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import duckdb
import yaml


REPO_ROOT = Path(__file__).resolve().parent.parent
OBSERVABILITY_CONFIG_PATH = REPO_ROOT / "configs" / "observability.yaml"
OBSERVABILITY_SCHEMA_PATH = REPO_ROOT / "warehouse" / "observability_schema.sql"
_INITIALIZED_DB_PATHS: set[str] = set()


def _load_observability_config() -> dict[str, Any]:
    if not OBSERVABILITY_CONFIG_PATH.exists():
        return {"database": {"path": "warehouse/bquant_observability.duckdb"}}
    with OBSERVABILITY_CONFIG_PATH.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def get_observability_db_path() -> str:
    config = _load_observability_config()
    path = config.get("database", {}).get("path", "warehouse/bquant_observability.duckdb")
    return str((REPO_ROOT / path).resolve())


def _duckdb_config_options(database_cfg: dict[str, Any]) -> dict[str, str]:
    """Build connection-time options for the observability DuckDB database.

    Args:
        database_cfg: `database` block from `configs/observability.yaml`.

    Returns:
        Mapping passed to `duckdb.connect(config=...)`.
    """
    options: dict[str, str] = {}
    if database_cfg.get("threads"):
        options["threads"] = str(int(database_cfg["threads"]))
    if database_cfg.get("memory_limit"):
        options["memory_limit"] = str(database_cfg["memory_limit"])
    return options


def _is_retryable_duckdb_error(exc: Exception) -> bool:
    """Return whether an observability DB connection error is transient."""
    message = str(exc).lower()
    retryable_markers = [
        "conflicting lock",
        "different configuration",
        "can't open a connection",
        "could not set lock",
        "database file is locked",
        "write-write conflict",
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
    """Open the observability DB with short backoff for lock races."""
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
    raise RuntimeError(f"Unable to connect to observability DuckDB at {db_path}") from last_exc


def get_observability_connection(read_only: bool = False) -> duckdb.DuckDBPyConnection:
    config = _load_observability_config()
    database_cfg = config.get("database", {})
    db_path = Path(get_observability_db_path())
    db_path.parent.mkdir(parents=True, exist_ok=True)

    return _connect_with_retry(
        db_path,
        read_only=read_only,
        config_options=_duckdb_config_options(database_cfg),
    )


def execute_observability_sql_file(sql_path: str | None = None) -> None:
    schema_path = Path(sql_path) if sql_path else OBSERVABILITY_SCHEMA_PATH
    if not schema_path.is_absolute():
        schema_path = (REPO_ROOT / schema_path).resolve()
    with schema_path.open("r", encoding="utf-8") as handle:
        sql = handle.read()
    with get_observability_connection(read_only=False) as conn:
        conn.execute(sql)


def ensure_observability_initialized() -> str:
    db_path = get_observability_db_path()
    if db_path in _INITIALIZED_DB_PATHS:
        return db_path
    execute_observability_sql_file()
    _INITIALIZED_DB_PATHS.add(db_path)
    return db_path
