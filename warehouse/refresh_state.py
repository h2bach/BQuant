"""Helpers for dataset refresh versioning in the main warehouse."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from warehouse.duckdb_connection import get_connection


DATASET_REFRESH_STATE_DDL = """
CREATE TABLE IF NOT EXISTS dataset_refresh_state (
    dataset_name VARCHAR PRIMARY KEY,
    refresh_version BIGINT NOT NULL DEFAULT 0,
    last_success_at TIMESTAMP,
    latest_data_ts TIMESTAMP,
    last_run_id VARCHAR,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_dataset_refresh_state_updated ON dataset_refresh_state(updated_at);
"""


def ensure_refresh_state_table() -> None:
    with get_connection(read_only=False) as conn:
        conn.execute(DATASET_REFRESH_STATE_DDL)


def _refresh_state_table_exists() -> bool:
    with get_connection(read_only=True) as conn:
        row = conn.execute(
            """
            SELECT count(*)
            FROM information_schema.tables
            WHERE table_schema = 'main'
              AND table_name = 'dataset_refresh_state'
            """
        ).fetchone()
    return bool(row and row[0])


def get_refresh_state(dataset_name: str) -> dict[str, Any]:
    if not _refresh_state_table_exists():
        ensure_refresh_state_table()
    with get_connection(read_only=True) as conn:
        row = conn.execute(
            """
            SELECT dataset_name, refresh_version, last_success_at, latest_data_ts, last_run_id, updated_at
            FROM dataset_refresh_state
            WHERE dataset_name = ?
            """,
            [dataset_name],
        ).fetchone()
    if row is None:
        return {
            "dataset_name": dataset_name,
            "refresh_version": 0,
            "last_success_at": None,
            "latest_data_ts": None,
            "last_run_id": None,
            "updated_at": None,
        }
    return {
        "dataset_name": row[0],
        "refresh_version": int(row[1]),
        "last_success_at": row[2],
        "latest_data_ts": row[3],
        "last_run_id": row[4],
        "updated_at": row[5],
    }


def get_refresh_version(dataset_name: str) -> int:
    return int(get_refresh_state(dataset_name).get("refresh_version", 0))


def bump_refresh_version(
    dataset_name: str,
    *,
    run_id: str,
    latest_data_ts: datetime | None,
    last_success_at: datetime | None = None,
) -> dict[str, Any]:
    ensure_refresh_state_table()
    event_time = last_success_at or datetime.now()
    with get_connection(read_only=False) as conn:
        current = conn.execute(
            "SELECT refresh_version FROM dataset_refresh_state WHERE dataset_name = ?",
            [dataset_name],
        ).fetchone()
        next_version = int(current[0]) + 1 if current else 1
        conn.execute(
            """
            INSERT INTO dataset_refresh_state (
                dataset_name,
                refresh_version,
                last_success_at,
                latest_data_ts,
                last_run_id,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT (dataset_name) DO UPDATE SET
                refresh_version = excluded.refresh_version,
                last_success_at = excluded.last_success_at,
                latest_data_ts = excluded.latest_data_ts,
                last_run_id = excluded.last_run_id,
                updated_at = excluded.updated_at
            """,
            [dataset_name, next_version, event_time, latest_data_ts, run_id, datetime.now()],
        )
    return get_refresh_state(dataset_name)
