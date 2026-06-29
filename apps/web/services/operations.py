"""Operations and alerts services for the BQuant web app."""

from __future__ import annotations

import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from utils.logger import BQuantLogger
from warehouse.duckdb_connection import get_connection
from warehouse.init_observability import initialize_observability
from warehouse.observability_connection import get_observability_connection, get_observability_db_path
from warehouse.refresh_state import get_refresh_state

from pipelines.live_update_runtime import market_session_state, now_local


REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
LOGGER = BQuantLogger("web_operations", component="web", subcomponent="operations", default_channel="web")


ACTION_MAP = {
    "run_intraday_delta": ["-m", "pipelines.run_intraday_delta", "--trigger-type", "manual"],
    "run_eod_reconcile": ["-m", "pipelines.run_eod_reconcile", "--trigger-type", "manual"],
    "run_dbt_transforms": ["-m", "pipelines.run_dbt_transforms", "--trigger-type", "manual"],
    "run_agent_cycle": ["-m", "pipelines.run_agent_cycle", "--trigger-type", "manual"],
    "ingest_observability_logs": ["-m", "pipelines.ingest_observability_logs"],
    "refresh_manifest": ["-m", "pipelines.refresh_manifest", "--trigger-type", "manual"],
    "evaluate_alerts": ["-m", "pipelines.evaluate_alerts", "--trigger-type", "manual"],
}


def ensure_operations_ready() -> None:
    """Initialize the observability store on first access if it is still absent."""
    if not Path(get_observability_db_path()).exists():
        initialize_observability()


def load_operations_snapshot() -> dict[str, Any]:
    """Assemble the current monitoring snapshot for the operations dashboard."""
    ensure_operations_ready()
    snapshot: dict[str, Any] = {
        "market_session_state": market_session_state(now_local()),
        "current_time": now_local().isoformat(),
        "worker_health": {},
        "last_intraday_job": {},
        "last_eod_job": {},
        "stale_symbol_count": 0,
        "failed_jobs_24h": 0,
        "source_lag_summary": [],
        "checkpoints": [],
        "recent_jobs": [],
        "recent_errors": [],
        "refresh_states": [],
    }

    with get_observability_connection(read_only=True) as conn:
        worker = conn.execute(
            """
            SELECT worker_name, last_heartbeat_ts, last_session_state, last_status
            FROM v_obs_worker_health
            ORDER BY last_heartbeat_ts DESC
            LIMIT 1
            """
        ).fetchone()
        if worker:
            snapshot["worker_health"] = {
                "worker_name": worker[0],
                "last_heartbeat_ts": str(worker[1]) if worker[1] else "N/A",
                "last_session_state": worker[2] or "N/A",
                "last_status": worker[3] or "N/A",
            }

        recent_jobs = conn.execute(
            """
            SELECT pipeline_name, status, trigger_type, start_time, end_time, duration_seconds, dataset_name, error_message
            FROM obs_job_runs
            ORDER BY coalesce(end_time, updated_at) DESC
            LIMIT 20
            """
        ).fetchall()
        snapshot["recent_jobs"] = [
            {
                "pipeline_name": row[0],
                "status": row[1],
                "trigger_type": row[2] or "N/A",
                "start_time": str(row[3]) if row[3] else "N/A",
                "end_time": str(row[4]) if row[4] else "N/A",
                "duration_seconds": f"{float(row[5]):.2f}" if row[5] is not None else "N/A",
                "dataset_name": row[6] or "N/A",
                "error_message": row[7] or "",
            }
            for row in recent_jobs
        ]

        for pipeline_name, key in [("run_intraday_delta", "last_intraday_job"), ("run_eod_reconcile", "last_eod_job")]:
            row = conn.execute(
                """
                SELECT pipeline_name, status, end_time, duration_seconds, error_message
                FROM obs_job_runs
                WHERE pipeline_name = ?
                ORDER BY coalesce(end_time, updated_at) DESC
                LIMIT 1
                """,
                [pipeline_name],
            ).fetchone()
            if row:
                snapshot[key] = {
                    "pipeline_name": row[0],
                    "status": row[1],
                    "end_time": str(row[2]) if row[2] else "N/A",
                    "duration_seconds": f"{float(row[3]):.2f}" if row[3] is not None else "N/A",
                    "error_message": row[4] or "",
                }

        failed_jobs_24h = conn.execute(
            """
            SELECT count(*)
            FROM obs_job_runs
            WHERE status = 'failed'
              AND coalesce(end_time, updated_at) >= now() - INTERVAL 1 DAY
            """
        ).fetchone()[0]
        snapshot["failed_jobs_24h"] = int(failed_jobs_24h)

        snapshot["source_lag_summary"] = [
            {
                "symbol": row[0],
                "provider": row[1] or "N/A",
                "last_request_end": str(row[2]) if row[2] else "N/A",
                "status": row[3] or "N/A",
                "rows_fetched": int(row[4] or 0),
            }
            for row in conn.execute(
                """
                SELECT symbol, provider, max(request_end), arg_max(status, request_end), arg_max(rows_fetched, request_end)
                FROM obs_source_requests
                WHERE dataset_name = 'intraday_ohlcv_15m_delta'
                GROUP BY symbol, provider
                ORDER BY symbol
                """
            ).fetchall()
        ]

        snapshot["checkpoints"] = [
            {
                "dataset_name": row[0],
                "symbol": row[1],
                "watermark_ts": str(row[2]) if row[2] else "N/A",
                "updated_at": str(row[3]) if row[3] else "N/A",
            }
            for row in conn.execute(
                """
                SELECT dataset_name, symbol, watermark_ts, updated_at
                FROM v_obs_symbol_lag
                ORDER BY dataset_name, symbol
                LIMIT 100
                """
            ).fetchall()
        ]

        snapshot["recent_errors"] = [
            {
                "event_ts": str(row[0]),
                "component": row[1],
                "subcomponent": row[2],
                "event_type": row[3],
                "run_id": row[4] or "",
                "dataset_name": row[5] or "",
                "symbol": row[6] or "",
                "status": row[7] or "",
                "message": row[8],
            }
            for row in conn.execute(
                """
                SELECT event_ts, component, subcomponent, event_type, run_id, dataset_name, symbol, status, message
                FROM v_obs_recent_failures
                LIMIT 20
                """
            ).fetchall()
        ]

    with get_connection(read_only=True) as conn:
        stale_count = conn.execute(
            """
            SELECT count(*)
            FROM data_file_manifest
            WHERE update_status IN ('stale', 'pending_merge', 'awaiting_refresh_window')
            """
        ).fetchone()[0]
    snapshot["stale_symbol_count"] = int(stale_count)
    snapshot["refresh_states"] = [
        get_refresh_state("daily_ohlcv_10y"),
        get_refresh_state("market_index_daily_10y"),
        get_refresh_state("intraday_ohlcv_15m_60d"),
        get_refresh_state("intraday_ohlcv_15m_delta"),
        get_refresh_state("analytics_mart_market_regime_daily"),
        get_refresh_state("analytics_mart_symbol_daily_features"),
        get_refresh_state("analytics_mart_symbol_data_quality"),
        get_refresh_state("analytics_mart_agent_context_daily"),
        get_refresh_state("agent_recommendations"),
    ]
    return snapshot


def load_alerts(
    *,
    status_filter: str = "all",
    severity_filter: str = "all",
    dataset_filter: str = "all",
    symbol_filter: str = "",
) -> list[dict[str, str]]:
    """Load alert rows using the current UI filters."""
    ensure_operations_ready()
    clauses = ["1=1"]
    params: list[Any] = []
    if status_filter != "all":
        clauses.append("status = ?")
        params.append(status_filter)
    if severity_filter != "all":
        clauses.append("severity = ?")
        params.append(severity_filter)
    if dataset_filter != "all":
        clauses.append("coalesce(dataset_name, '') = ?")
        params.append("" if dataset_filter == "none" else dataset_filter)
    if symbol_filter.strip():
        clauses.append("upper(coalesce(symbol, '')) = ?")
        params.append(symbol_filter.strip().upper())
    where_clause = " AND ".join(clauses)

    with get_observability_connection(read_only=True) as conn:
        rows = conn.execute(
            f"""
            SELECT alert_key, alert_type, severity, dataset_name, symbol, status, message, first_event_ts, last_event_ts, acknowledged_at, resolved_at
            FROM obs_alerts
            WHERE {where_clause}
            ORDER BY updated_at DESC
            LIMIT 200
            """,
            params,
        ).fetchall()
    return [
        {
            "alert_key": row[0],
            "alert_type": row[1],
            "severity": row[2],
            "dataset_name": row[3] or "",
            "symbol": row[4] or "",
            "status": row[5],
            "message": row[6],
            "first_event_ts": str(row[7]) if row[7] else "",
            "last_event_ts": str(row[8]) if row[8] else "",
            "acknowledged_at": str(row[9]) if row[9] else "",
            "resolved_at": str(row[10]) if row[10] else "",
        }
        for row in rows
    ]


def alert_keys(status_filter: str = "open") -> list[str]:
    """Return alert keys for dropdown selection, optionally filtered by status."""
    ensure_operations_ready()
    sql = "SELECT alert_key FROM obs_alerts"
    params: list[Any] = []
    if status_filter != "all":
        sql += " WHERE status = ?"
        params.append(status_filter)
    sql += " ORDER BY updated_at DESC"
    with get_observability_connection(read_only=True) as conn:
        rows = conn.execute(sql, params).fetchall()
    return [str(row[0]) for row in rows if row and row[0]]


def trigger_action(action_name: str) -> int:
    """Spawn a supported maintenance action and return the child PID."""
    ensure_operations_ready()
    if action_name not in ACTION_MAP:
        raise ValueError(f"Unsupported operations action: {action_name}")
    command = [sys.executable, *ACTION_MAP[action_name]]
    process = subprocess.Popen(
        command,
        cwd=str(REPO_ROOT),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    LOGGER.log_web_event(
        f"Triggered operations action: {action_name}",
        event_type="manual_trigger",
        status="submitted",
        action_name=action_name,
        pid=int(process.pid),
    )
    return int(process.pid)


def trigger_alert_transition(alert_key: str, target_status: str) -> int:
    """Submit an alert acknowledgement or resolution action."""
    ensure_operations_ready()
    if target_status not in {"acknowledged", "resolved"}:
        raise ValueError(f"Unsupported alert transition: {target_status}")
    flag = "--ack" if target_status == "acknowledged" else "--resolve"
    command = [sys.executable, "-m", "pipelines.evaluate_alerts", flag, alert_key]
    process = subprocess.Popen(
        command,
        cwd=str(REPO_ROOT),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    LOGGER.log_web_event(
        f"Triggered alert transition to {target_status}",
        event_type="alert_transition",
        status="submitted",
        alert_key=alert_key,
        target_status=target_status,
        pid=int(process.pid),
    )
    return int(process.pid)
