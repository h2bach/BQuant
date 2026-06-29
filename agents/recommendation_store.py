"""Persistence helpers for BQuant agent recommendations."""

from __future__ import annotations

from datetime import date
from typing import Any

import pandas as pd

from warehouse.duckdb_connection import get_connection


def ensure_agent_tables() -> None:
    """Create agent recommendation tables and views if they are missing.

    Side Effects:
        Executes idempotent DDL in the primary BQuant DuckDB warehouse.
    """
    with get_connection(read_only=False) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS agent_recommendation_runs (
                run_id VARCHAR PRIMARY KEY,
                as_of_date DATE NOT NULL,
                trigger_type VARCHAR NOT NULL,
                status VARCHAR NOT NULL,
                input_rows INTEGER NOT NULL DEFAULT 0,
                output_rows INTEGER NOT NULL DEFAULT 0,
                agent_version VARCHAR NOT NULL,
                optimizer_mode VARCHAR,
                qaoa_enabled BOOLEAN NOT NULL DEFAULT FALSE,
                notes VARCHAR,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS agent_recommendations (
                run_id VARCHAR NOT NULL,
                as_of_date DATE NOT NULL,
                symbol VARCHAR NOT NULL,
                recommendation VARCHAR NOT NULL,
                confidence DOUBLE NOT NULL,
                score DOUBLE NOT NULL,
                risk_level VARCHAR NOT NULL,
                suggested_weight DOUBLE NOT NULL DEFAULT 0,
                rationale_json VARCHAR NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (run_id, symbol)
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_agent_recommendations_date
            ON agent_recommendations(as_of_date)
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_agent_recommendations_symbol
            ON agent_recommendations(symbol)
            """
        )
        conn.execute(
            """
            CREATE OR REPLACE VIEW v_latest_agent_recommendations AS
            WITH latest_run AS (
                SELECT run_id
                FROM agent_recommendation_runs
                WHERE status = 'success'
                ORDER BY created_at DESC
                LIMIT 1
            )
            SELECT recommendations.*
            FROM agent_recommendations recommendations
            INNER JOIN latest_run
              ON recommendations.run_id = latest_run.run_id
            ORDER BY recommendations.suggested_weight DESC, recommendations.score DESC, recommendations.symbol
            """
        )


def record_agent_run(
    *,
    run_id: str,
    as_of_date: date,
    trigger_type: str,
    status: str,
    input_rows: int,
    output_rows: int,
    agent_version: str,
    optimizer_mode: str | None,
    qaoa_enabled: bool,
    notes: str | None = None,
) -> None:
    """Persist one agent cycle run summary.

    Args:
        run_id: Unique agent cycle id.
        as_of_date: Trading date represented by the input context.
        trigger_type: `manual`, `scheduled`, or `recovery`.
        status: Final run status.
        input_rows: Context rows read.
        output_rows: Recommendation rows written.
        agent_version: Version label for deterministic scoring logic.
        optimizer_mode: Portfolio optimizer mode.
        qaoa_enabled: Whether QAOA optimization was enabled.
        notes: Optional diagnostic text.
    """
    ensure_agent_tables()
    with get_connection(read_only=False) as conn:
        conn.execute(
            """
            INSERT INTO agent_recommendation_runs (
                run_id,
                as_of_date,
                trigger_type,
                status,
                input_rows,
                output_rows,
                agent_version,
                optimizer_mode,
                qaoa_enabled,
                notes
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (run_id) DO UPDATE SET
                status = excluded.status,
                input_rows = excluded.input_rows,
                output_rows = excluded.output_rows,
                optimizer_mode = excluded.optimizer_mode,
                qaoa_enabled = excluded.qaoa_enabled,
                notes = excluded.notes
            """,
            [
                run_id,
                as_of_date,
                trigger_type,
                status,
                int(input_rows),
                int(output_rows),
                agent_version,
                optimizer_mode,
                bool(qaoa_enabled),
                notes,
            ],
        )


def write_recommendations(frame: pd.DataFrame) -> int:
    """Write recommendation rows to the warehouse.

    Args:
        frame: Recommendation rows matching `agent_recommendations`.

    Returns:
        Number of rows inserted.
    """
    ensure_agent_tables()
    if frame.empty:
        return 0
    with get_connection(read_only=False) as conn:
        conn.register("tmp_agent_recommendations", frame)
        conn.execute(
            """
            INSERT INTO agent_recommendations (
                run_id,
                as_of_date,
                symbol,
                recommendation,
                confidence,
                score,
                risk_level,
                suggested_weight,
                rationale_json
            )
            SELECT
                run_id,
                as_of_date,
                symbol,
                recommendation,
                confidence,
                score,
                risk_level,
                suggested_weight,
                rationale_json
            FROM tmp_agent_recommendations
            ON CONFLICT (run_id, symbol) DO UPDATE SET
                recommendation = excluded.recommendation,
                confidence = excluded.confidence,
                score = excluded.score,
                risk_level = excluded.risk_level,
                suggested_weight = excluded.suggested_weight,
                rationale_json = excluded.rationale_json
            """
        )
        conn.unregister("tmp_agent_recommendations")
    return int(len(frame))
