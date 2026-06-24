"""Ingest structured JSONL observability logs into the observability DuckDB."""

from __future__ import annotations

import argparse
import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from utils.logger import BQuantLogger
from warehouse.init_observability import initialize_observability
from warehouse.observability_connection import get_observability_connection


PIPELINE_NAME = "ingest_observability_logs"
LOGGER = BQuantLogger(PIPELINE_NAME, component="pipeline", subcomponent=PIPELINE_NAME, default_channel="pipeline")


REQUIRED_EVENT_FIELDS = {
    "event_id",
    "event_ts",
    "level",
    "component",
    "subcomponent",
    "channel",
    "event_type",
    "status",
    "message",
    "payload_json",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Ingest observability JSONL logs into DuckDB.")
    parser.add_argument("--channel", choices=["scheduler", "pipeline", "ingestion", "web", "alerts"], help="Optional channel filter.")
    return parser.parse_args()


def _log_root(channel: str | None = None) -> Path:
    initialize_observability()
    root = Path(LOGGER.paths.observability_jsonl_dir)
    return root / channel if channel else root


def _iter_log_files(channel: str | None = None) -> list[Path]:
    root = _log_root(channel)
    if channel:
        return sorted(root.glob("*.jsonl"))
    return sorted(root.glob("*/*.jsonl"))


def _read_offsets(conn) -> dict[str, int]:
    rows = conn.execute(
        """
        SELECT file_path, last_line_number
        FROM obs_ingestion_offsets
        """
    ).fetchall()
    return {str(file_path): int(last_line_number) for file_path, last_line_number in rows}


def _upsert_offset(conn, file_path: str, last_line_number: int) -> None:
    conn.execute(
        """
        INSERT INTO obs_ingestion_offsets (file_path, last_line_number, last_ingested_at, updated_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT (file_path) DO UPDATE SET
            last_line_number = excluded.last_line_number,
            last_ingested_at = excluded.last_ingested_at,
            updated_at = excluded.updated_at
        """,
        [file_path, last_line_number, datetime.now(), datetime.now()],
    )


def _write_dead_letter(conn, file_path: str, line_number: int, raw_line: str, error_message: str) -> None:
    conn.execute(
        """
        INSERT INTO obs_dead_letter_events (dead_letter_id, file_path, line_number, raw_line, error_message, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        [str(uuid.uuid4()), file_path, line_number, raw_line[:65535], error_message[:2048], datetime.now()],
    )


def _validate_event(event: dict[str, Any]) -> None:
    missing = sorted(REQUIRED_EVENT_FIELDS - set(event))
    if missing:
        raise ValueError(f"Missing required fields: {missing}")


def _event_payload(event: dict[str, Any]) -> dict[str, Any]:
    payload = event.get("payload")
    if isinstance(payload, dict):
        return payload
    payload_json = event.get("payload_json")
    if isinstance(payload_json, str) and payload_json:
        try:
            loaded = json.loads(payload_json)
            return loaded if isinstance(loaded, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def _insert_log_event(conn, event: dict[str, Any], file_path: str, line_number: int) -> None:
    payload_json = event.get("payload_json")
    if not isinstance(payload_json, str):
        payload_json = json.dumps(_event_payload(event), ensure_ascii=True, sort_keys=True)
    conn.execute(
        """
        INSERT INTO obs_log_events (
            event_id,
            event_ts,
            level,
            component,
            subcomponent,
            channel,
            event_type,
            run_id,
            trigger_type,
            dataset_name,
            symbol,
            status,
            message,
            payload_json,
            source_file,
            source_line_number,
            created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (event_id) DO NOTHING
        """,
        [
            str(event["event_id"]),
            datetime.fromisoformat(str(event["event_ts"])),
            str(event["level"]),
            str(event["component"]),
            str(event.get("subcomponent") or ""),
            str(event["channel"]),
            str(event["event_type"]),
            event.get("run_id"),
            event.get("trigger_type"),
            event.get("dataset_name"),
            event.get("symbol"),
            event.get("status"),
            str(event["message"]),
            payload_json,
            file_path,
            line_number,
            datetime.now(),
        ],
    )


def _project_job_run(conn, event: dict[str, Any], payload: dict[str, Any]) -> None:
    run_id = event.get("run_id")
    pipeline_name = payload.get("pipeline_name") or payload.get("operation") or event.get("subcomponent")
    if not run_id or not pipeline_name:
        return
    conn.execute(
        """
        INSERT INTO obs_job_runs (
            run_id,
            pipeline_name,
            trigger_type,
            status,
            start_time,
            end_time,
            duration_seconds,
            dataset_name,
            input_rows,
            output_rows,
            error_message,
            source_event_id,
            updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (run_id) DO UPDATE SET
            pipeline_name = excluded.pipeline_name,
            trigger_type = excluded.trigger_type,
            status = excluded.status,
            start_time = excluded.start_time,
            end_time = excluded.end_time,
            duration_seconds = excluded.duration_seconds,
            dataset_name = excluded.dataset_name,
            input_rows = excluded.input_rows,
            output_rows = excluded.output_rows,
            error_message = excluded.error_message,
            source_event_id = excluded.source_event_id,
            updated_at = excluded.updated_at
        """,
        [
            str(run_id),
            str(pipeline_name),
            event.get("trigger_type"),
            event.get("status"),
            datetime.fromisoformat(payload["start_time"]) if payload.get("start_time") else None,
            datetime.fromisoformat(payload["end_time"]) if payload.get("end_time") else None,
            payload.get("duration_seconds"),
            event.get("dataset_name") or payload.get("dataset_name"),
            payload.get("rows_fetched") or payload.get("input_rows"),
            payload.get("rows_saved") or payload.get("output_rows"),
            payload.get("error_message"),
            event.get("event_id"),
            datetime.now(),
        ],
    )


def _project_source_request(conn, event: dict[str, Any], payload: dict[str, Any]) -> None:
    request_id = str(payload.get("request_id") or event["event_id"])
    conn.execute(
        """
        INSERT INTO obs_source_requests (
            request_id,
            run_id,
            dataset_name,
            symbol,
            provider,
            request_start,
            request_end,
            duration_seconds,
            status,
            rows_fetched,
            wait_seconds,
            empty_payload,
            error_type,
            error_message,
            source_event_id,
            updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (request_id) DO UPDATE SET
            run_id = excluded.run_id,
            dataset_name = excluded.dataset_name,
            symbol = excluded.symbol,
            provider = excluded.provider,
            request_start = excluded.request_start,
            request_end = excluded.request_end,
            duration_seconds = excluded.duration_seconds,
            status = excluded.status,
            rows_fetched = excluded.rows_fetched,
            wait_seconds = excluded.wait_seconds,
            empty_payload = excluded.empty_payload,
            error_type = excluded.error_type,
            error_message = excluded.error_message,
            source_event_id = excluded.source_event_id,
            updated_at = excluded.updated_at
        """,
        [
            request_id,
            event.get("run_id"),
            event.get("dataset_name"),
            event.get("symbol"),
            payload.get("provider"),
            datetime.fromisoformat(payload["request_start"]) if payload.get("request_start") else None,
            datetime.fromisoformat(payload["request_end"]) if payload.get("request_end") else None,
            payload.get("duration_seconds"),
            event.get("status"),
            payload.get("rows_fetched"),
            payload.get("wait_seconds"),
            bool(payload.get("empty_payload", False)),
            payload.get("error_type"),
            payload.get("error_message"),
            event.get("event_id"),
            datetime.now(),
        ],
    )


def _project_checkpoint(conn, event: dict[str, Any], payload: dict[str, Any]) -> None:
    dataset_name = event.get("dataset_name")
    symbol = event.get("symbol")
    if not dataset_name or not symbol:
        return
    watermark = payload.get("watermark_ts")
    conn.execute(
        """
        INSERT INTO obs_live_checkpoints (
            dataset_name,
            symbol,
            watermark_ts,
            last_run_id,
            trigger_type,
            status,
            updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (dataset_name, symbol) DO UPDATE SET
            watermark_ts = excluded.watermark_ts,
            last_run_id = excluded.last_run_id,
            trigger_type = excluded.trigger_type,
            status = excluded.status,
            updated_at = excluded.updated_at
        """,
        [
            dataset_name,
            symbol,
            datetime.fromisoformat(str(watermark)) if watermark else None,
            event.get("run_id"),
            event.get("trigger_type"),
            event.get("status"),
            datetime.now(),
        ],
    )


def _project_heartbeat(conn, event: dict[str, Any], payload: dict[str, Any]) -> None:
    conn.execute(
        """
        INSERT INTO obs_scheduler_heartbeats (
            heartbeat_id,
            worker_name,
            heartbeat_ts,
            session_state,
            trigger_type,
            status,
            payload_json,
            source_event_id,
            created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (heartbeat_id) DO NOTHING
        """,
        [
            str(event["event_id"]),
            str(payload.get("worker_name") or event.get("subcomponent") or "worker"),
            datetime.fromisoformat(str(event["event_ts"])),
            payload.get("session_state"),
            event.get("trigger_type"),
            event.get("status"),
            event.get("payload_json"),
            event.get("event_id"),
            datetime.now(),
        ],
    )


def _project_alert(conn, event: dict[str, Any], payload: dict[str, Any]) -> None:
    alert_key = str(payload.get("alert_key") or "")
    alert_type = str(payload.get("alert_type") or "")
    if not alert_key or not alert_type:
        return
    current = conn.execute(
        """
        SELECT first_event_ts, acknowledged_at, resolved_at
        FROM obs_alerts
        WHERE alert_key = ?
        """,
        [alert_key],
    ).fetchone()
    status = str(event.get("status") or payload.get("status") or "open")
    first_event_ts = current[0] if current else datetime.fromisoformat(str(event["event_ts"]))
    acknowledged_at = current[1] if current else None
    resolved_at = current[2] if current else None
    event_ts = datetime.fromisoformat(str(event["event_ts"]))
    if status == "acknowledged":
        acknowledged_at = event_ts
        resolved_at = None
    elif status == "resolved":
        resolved_at = event_ts
    else:
        acknowledged_at = None
        resolved_at = None
    conn.execute(
        """
        INSERT INTO obs_alerts (
            alert_key,
            alert_type,
            severity,
            dataset_name,
            symbol,
            status,
            message,
            first_event_ts,
            last_event_ts,
            acknowledged_at,
            resolved_at,
            last_run_id,
            last_event_id,
            payload_json,
            updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (alert_key) DO UPDATE SET
            alert_type = excluded.alert_type,
            severity = excluded.severity,
            dataset_name = excluded.dataset_name,
            symbol = excluded.symbol,
            status = excluded.status,
            message = excluded.message,
            first_event_ts = coalesce(obs_alerts.first_event_ts, excluded.first_event_ts),
            last_event_ts = excluded.last_event_ts,
            acknowledged_at = excluded.acknowledged_at,
            resolved_at = excluded.resolved_at,
            last_run_id = excluded.last_run_id,
            last_event_id = excluded.last_event_id,
            payload_json = excluded.payload_json,
            updated_at = excluded.updated_at
        """,
        [
            alert_key,
            alert_type,
            str(payload.get("severity") or event.get("level") or "WARNING"),
            event.get("dataset_name"),
            event.get("symbol"),
            status,
            event.get("message"),
            first_event_ts,
            event_ts,
            acknowledged_at,
            resolved_at,
            event.get("run_id"),
            event.get("event_id"),
            event.get("payload_json"),
            datetime.now(),
        ],
    )


def _project_event(conn, event: dict[str, Any]) -> None:
    payload = _event_payload(event)
    event_type = str(event.get("event_type"))
    if event_type == "pipeline_run":
        _project_job_run(conn, event, payload)
    elif event_type == "source_request":
        _project_source_request(conn, event, payload)
    elif event_type == "checkpoint_updated":
        _project_checkpoint(conn, event, payload)
    elif event_type == "scheduler_heartbeat":
        _project_heartbeat(conn, event, payload)
    elif event_type == "alert_state_change":
        _project_alert(conn, event, payload)


def ingest_observability_logs(*, channel: str | None = None) -> dict[str, int]:
    initialize_observability()
    totals = {"files": 0, "events": 0, "dead_letters": 0}
    with get_observability_connection(read_only=False) as conn:
        offsets = _read_offsets(conn)
        for log_file in _iter_log_files(channel):
            totals["files"] += 1
            last_line = offsets.get(str(log_file), 0)
            newest_line = last_line
            with log_file.open("r", encoding="utf-8") as handle:
                for line_number, raw_line in enumerate(handle, start=1):
                    if line_number <= last_line:
                        continue
                    newest_line = line_number
                    try:
                        event = json.loads(raw_line)
                        if not isinstance(event, dict):
                            raise ValueError("Event line is not a JSON object")
                        _validate_event(event)
                        _insert_log_event(conn, event, str(log_file), line_number)
                        _project_event(conn, event)
                        totals["events"] += 1
                    except Exception as exc:  # pragma: no cover - diagnostic path
                        _write_dead_letter(conn, str(log_file), line_number, raw_line.strip(), str(exc))
                        totals["dead_letters"] += 1
            _upsert_offset(conn, str(log_file), newest_line)
    return totals


def main() -> None:
    args = parse_args()
    run_id = str(uuid.uuid4())
    logger = LOGGER.with_run_context(run_id=run_id, trigger_type="manual")
    started_at = datetime.now()
    try:
        logger.info(
            "Starting observability log ingestion",
            event_type="job_start",
            status="running",
            channel="pipeline",
        )
        totals = ingest_observability_logs(channel=args.channel)
        finished_at = datetime.now()
        logger.log_pipeline_run(
            pipeline_name=PIPELINE_NAME,
            start_time=started_at.isoformat(),
            end_time=finished_at.isoformat(),
            status="success",
            steps_completed=["ingest_observability_logs"],
            steps_failed=[],
            run_id=run_id,
            trigger_type="manual",
            files_processed=totals["files"],
            events_ingested=totals["events"],
            dead_letters=totals["dead_letters"],
        )
    except Exception as exc:
        finished_at = datetime.now()
        logger.log_error(
            PIPELINE_NAME,
            type(exc).__name__,
            str(exc),
            context={"run_id": run_id, "trigger_type": "manual"},
        )
        logger.log_pipeline_run(
            pipeline_name=PIPELINE_NAME,
            start_time=started_at.isoformat(),
            end_time=finished_at.isoformat(),
            status="failed",
            steps_completed=[],
            steps_failed=["ingest_observability_logs"],
            error_message=str(exc),
            run_id=run_id,
            trigger_type="manual",
        )
        raise


if __name__ == "__main__":
    main()
