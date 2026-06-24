"""Evaluate live-update alert rules and emit alert state change events."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta
from typing import Any

from utils.logger import BQuantLogger
from warehouse.duckdb_connection import get_connection
from warehouse.init_observability import initialize_observability
from warehouse.observability_connection import get_observability_connection

from pipelines.ingest_observability_logs import ingest_observability_logs
from pipelines.live_update_runtime import (
    create_run_id,
    eod_reconcile_time,
    get_latest_daily_date,
    get_plot_floor_timestamp,
    is_trading_day,
    latest_eligible_intraday_slot,
    load_live_update_config,
    market_timezone,
    now_local,
    record_pipeline_run,
)


PIPELINE_NAME = "evaluate_alerts"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Evaluate alert rules for BQuant live update.")
    parser.add_argument("--trigger-type", default="manual", choices=["manual", "scheduled", "recovery"])
    parser.add_argument("--ack", help="Acknowledge alert key.")
    parser.add_argument("--resolve", help="Resolve alert key.")
    return parser.parse_args()


def _load_thresholds() -> dict[str, Any]:
    config = load_live_update_config()
    obs_cfg = {}
    # Avoid importing yaml again: logger config already exists, but explicit values live in observability.yaml.
    initialize_observability()
    from utils.logger import _load_observability_config  # local import to avoid circular init

    obs_cfg = _load_observability_config()
    return obs_cfg.get("alerts", {})


def _current_alert_status() -> dict[str, str]:
    initialize_observability()
    with get_observability_connection(read_only=True) as conn:
        rows = conn.execute("SELECT alert_key, status FROM obs_alerts").fetchall()
    return {str(alert_key): str(status) for alert_key, status in rows}


def _emit_alert_state_change(
    logger: BQuantLogger,
    *,
    alert_key: str,
    alert_type: str,
    severity: str,
    status: str,
    message: str,
    dataset_name: str | None = None,
    symbol: str | None = None,
    **payload: Any,
) -> None:
    logger.log_alert_event(
        message,
        event_type="alert_state_change",
        status=status,
        dataset_name=dataset_name,
        symbol=symbol,
        alert_key=alert_key,
        alert_type=alert_type,
        severity=severity,
        **payload,
    )


def _evaluate_worker_heartbeat(logger: BQuantLogger, current_status: dict[str, str]) -> None:
    thresholds = _load_thresholds()
    grace = int(thresholds.get("heartbeat_lag_seconds", 120))
    with get_observability_connection(read_only=True) as conn:
        row = conn.execute(
            """
            SELECT worker_name, max(heartbeat_ts)
            FROM obs_scheduler_heartbeats
            GROUP BY worker_name
            ORDER BY max(heartbeat_ts) DESC
            LIMIT 1
            """
        ).fetchone()

    alert_key = "worker_heartbeat_lag"
    if row is None:
        if current_status.get(alert_key) != "open":
            _emit_alert_state_change(
                logger,
                alert_key=alert_key,
                alert_type="worker_heartbeat_lag",
                severity="WARNING",
                status="open",
                message="Live update worker has not emitted any heartbeat yet.",
                worker_name="unknown",
            )
        return

    worker_name, last_heartbeat = row
    last_heartbeat = last_heartbeat.replace(tzinfo=market_timezone()) if last_heartbeat.tzinfo is None else last_heartbeat
    lag_seconds = (now_local() - last_heartbeat).total_seconds()
    if lag_seconds > grace:
        if current_status.get(alert_key) != "open":
            _emit_alert_state_change(
                logger,
                alert_key=alert_key,
                alert_type="worker_heartbeat_lag",
                severity="ERROR",
                status="open",
                message=f"Worker heartbeat is stale by {int(lag_seconds)} seconds.",
                worker_name=worker_name,
                lag_seconds=int(lag_seconds),
            )
    elif current_status.get(alert_key) in {"open", "acknowledged"}:
        _emit_alert_state_change(
            logger,
            alert_key=alert_key,
            alert_type="worker_heartbeat_lag",
            severity="INFO",
            status="resolved",
            message="Worker heartbeat recovered.",
            worker_name=worker_name,
            lag_seconds=int(lag_seconds),
        )


def _evaluate_intraday_lag(logger: BQuantLogger, current_status: dict[str, str]) -> None:
    current = now_local()
    if not is_trading_day(current):
        return
    latest_slot = latest_eligible_intraday_slot(current)
    if latest_slot is None:
        return
    lag_minutes = int(_load_thresholds().get("intraday_lag_minutes", 30))
    floor_ts = get_plot_floor_timestamp()
    alert_key = "intraday_dataset_lag"
    if floor_ts is None:
        if current_status.get(alert_key) != "open":
            _emit_alert_state_change(
                logger,
                alert_key=alert_key,
                alert_type="intraday_lag",
                severity="ERROR",
                status="open",
                message="Intraday plot dataset has no bars.",
                dataset_name="intraday_ohlcv_15m_delta",
            )
        return
    lag = latest_slot.replace(tzinfo=None) - floor_ts.replace(tzinfo=None)
    if lag > timedelta(minutes=lag_minutes):
        if current_status.get(alert_key) != "open":
            _emit_alert_state_change(
                logger,
                alert_key=alert_key,
                alert_type="intraday_lag",
                severity="ERROR",
                status="open",
                message=f"Intraday coverage is lagging by {int(lag.total_seconds() // 60)} minutes.",
                dataset_name="intraday_ohlcv_15m_delta",
                latest_slot=latest_slot.isoformat(),
                floor_timestamp=floor_ts.isoformat(),
            )
    elif current_status.get(alert_key) in {"open", "acknowledged"}:
        _emit_alert_state_change(
            logger,
            alert_key=alert_key,
            alert_type="intraday_lag",
            severity="INFO",
            status="resolved",
            message="Intraday coverage is back within freshness threshold.",
            dataset_name="intraday_ohlcv_15m_delta",
            latest_slot=latest_slot.isoformat(),
            floor_timestamp=floor_ts.isoformat(),
        )


def _evaluate_daily_eod(logger: BQuantLogger, current_status: dict[str, str]) -> None:
    current = now_local()
    if not is_trading_day(current):
        return
    if current < eod_reconcile_time(current.date()):
        return
    latest_daily = get_latest_daily_date()
    alert_key = "daily_eod_missing"
    if latest_daily is None or latest_daily < current.date():
        if current_status.get(alert_key) != "open":
            _emit_alert_state_change(
                logger,
                alert_key=alert_key,
                alert_type="daily_eod_missing",
                severity="ERROR",
                status="open",
                message="Daily 10-year base has not been refreshed for the current trading day after EOD cutoff.",
                dataset_name="daily_ohlcv_10y",
                latest_daily=str(latest_daily) if latest_daily else None,
            )
    elif current_status.get(alert_key) in {"open", "acknowledged"}:
        _emit_alert_state_change(
            logger,
            alert_key=alert_key,
            alert_type="daily_eod_missing",
            severity="INFO",
            status="resolved",
            message="Daily EOD dataset is refreshed for the current trading day.",
            dataset_name="daily_ohlcv_10y",
            latest_daily=str(latest_daily),
        )


def _evaluate_consecutive_failures(logger: BQuantLogger, current_status: dict[str, str]) -> None:
    threshold = int(_load_thresholds().get("consecutive_job_failures", 2))
    with get_observability_connection(read_only=True) as conn:
        pipelines = [
            row[0]
            for row in conn.execute("SELECT DISTINCT pipeline_name FROM obs_job_runs").fetchall()
            if row and row[0]
        ]
        for pipeline_name in pipelines:
            rows = conn.execute(
                """
                SELECT status
                FROM obs_job_runs
                WHERE pipeline_name = ?
                ORDER BY coalesce(end_time, updated_at) DESC
                LIMIT ?
                """,
                [pipeline_name, threshold],
            ).fetchall()
            statuses = [str(status) for status, in rows]
            alert_key = f"consecutive_failures::{pipeline_name}"
            if len(statuses) == threshold and all(status == "failed" for status in statuses):
                if current_status.get(alert_key) != "open":
                    _emit_alert_state_change(
                        logger,
                        alert_key=alert_key,
                        alert_type="consecutive_failures",
                        severity="ERROR",
                        status="open",
                        message=f"{pipeline_name} failed {threshold} runs in a row.",
                        pipeline_name=pipeline_name,
                    )
            elif current_status.get(alert_key) in {"open", "acknowledged"}:
                _emit_alert_state_change(
                    logger,
                    alert_key=alert_key,
                    alert_type="consecutive_failures",
                    severity="INFO",
                    status="resolved",
                    message=f"{pipeline_name} is no longer failing consecutively.",
                    pipeline_name=pipeline_name,
                )


def _evaluate_manifest_mismatch(logger: BQuantLogger, current_status: dict[str, str]) -> None:
    with get_connection(read_only=True) as conn:
        daily_mismatches = conn.execute(
            """
            WITH actual AS (
                SELECT symbol, max(trading_date) AS actual_end, count(*) AS actual_rows
                FROM daily_ohlcv_base
                GROUP BY symbol
            )
            SELECT count(*)
            FROM data_file_manifest m
            INNER JOIN actual a ON m.symbol = a.symbol
            WHERE m.dataset_name = 'daily_ohlcv_10y'
              AND (m.coverage_end::DATE != a.actual_end OR m.row_count != a.actual_rows)
            """
        ).fetchone()[0]
        intraday_mismatches = conn.execute(
            """
            WITH actual AS (
                SELECT symbol, max(bar_time) AS actual_end, count(*) AS actual_rows
                FROM intraday_ohlcv_15m_base
                GROUP BY symbol
            )
            SELECT count(*)
            FROM data_file_manifest m
            INNER JOIN actual a ON m.symbol = a.symbol
            WHERE m.dataset_name = 'intraday_ohlcv_15m_60d'
              AND (m.coverage_end != a.actual_end OR m.row_count != a.actual_rows)
            """
        ).fetchone()[0]
    total_mismatches = int(daily_mismatches) + int(intraday_mismatches)
    alert_key = "manifest_table_mismatch"
    if total_mismatches > 0:
        if current_status.get(alert_key) != "open":
            _emit_alert_state_change(
                logger,
                alert_key=alert_key,
                alert_type="manifest_mismatch",
                severity="WARNING",
                status="open",
                message=f"Manifest/file metadata differs from table coverage for {total_mismatches} symbol-datasets.",
                mismatch_count=total_mismatches,
            )
    elif current_status.get(alert_key) in {"open", "acknowledged"}:
        _emit_alert_state_change(
            logger,
            alert_key=alert_key,
            alert_type="manifest_mismatch",
            severity="INFO",
            status="resolved",
            message="Manifest metadata matches table coverage again.",
            mismatch_count=0,
        )


def _evaluate_pending_merge(logger: BQuantLogger, current_status: dict[str, str]) -> None:
    current = now_local().date()
    with get_connection(read_only=True) as conn:
        pending_rows = conn.execute(
            """
            SELECT count(*)
            FROM data_file_manifest
            WHERE dataset_name = 'intraday_ohlcv_15m_delta'
              AND update_status = 'pending_merge'
              AND snapshot_date IS NOT NULL
              AND snapshot_date < ?
            """,
            [current],
        ).fetchone()[0]
    alert_key = "delta_pending_merge"
    if int(pending_rows) > 0:
        if current_status.get(alert_key) != "open":
            _emit_alert_state_change(
                logger,
                alert_key=alert_key,
                alert_type="pending_merge",
                severity="WARNING",
                status="open",
                message="Intraday delta still has pending merge rows from a previous session.",
                dataset_name="intraday_ohlcv_15m_delta",
                pending_rows=int(pending_rows),
            )
    elif current_status.get(alert_key) in {"open", "acknowledged"}:
        _emit_alert_state_change(
            logger,
            alert_key=alert_key,
            alert_type="pending_merge",
            severity="INFO",
            status="resolved",
            message="Intraday delta pending merge backlog is cleared.",
            dataset_name="intraday_ohlcv_15m_delta",
            pending_rows=0,
        )


def _evaluate_empty_payloads(logger: BQuantLogger, current_status: dict[str, str]) -> None:
    threshold = int(_load_thresholds().get("empty_payload_consecutive_slots", 2))
    with get_observability_connection(read_only=True) as conn:
        symbols = [
            row[0]
            for row in conn.execute(
                """
                SELECT DISTINCT symbol
                FROM obs_source_requests
                WHERE dataset_name = 'intraday_ohlcv_15m_delta'
                  AND symbol IS NOT NULL
                ORDER BY symbol
                """
            ).fetchall()
            if row and row[0]
        ]
        for symbol in symbols:
            rows = conn.execute(
                """
                SELECT empty_payload, status
                FROM obs_source_requests
                WHERE dataset_name = 'intraday_ohlcv_15m_delta'
                  AND symbol = ?
                ORDER BY coalesce(request_end, updated_at) DESC
                LIMIT ?
                """,
                [symbol, threshold],
            ).fetchall()
            values = [(bool(empty_payload), str(status)) for empty_payload, status in rows]
            alert_key = f"empty_payload::{symbol}"
            should_open = len(values) == threshold and all(item[0] and item[1] == "empty" for item in values)
            if should_open and current_status.get(alert_key) != "open":
                _emit_alert_state_change(
                    logger,
                    alert_key=alert_key,
                    alert_type="empty_payload",
                    severity="WARNING",
                    status="open",
                    message=f"{symbol} returned empty payload for {threshold} consecutive intraday pulls.",
                    dataset_name="intraday_ohlcv_15m_delta",
                    symbol=symbol,
                )
            elif not should_open and current_status.get(alert_key) in {"open", "acknowledged"}:
                _emit_alert_state_change(
                    logger,
                    alert_key=alert_key,
                    alert_type="empty_payload",
                    severity="INFO",
                    status="resolved",
                    message=f"{symbol} is receiving intraday payloads again.",
                    dataset_name="intraday_ohlcv_15m_delta",
                    symbol=symbol,
                )


def _manual_transition(logger: BQuantLogger, alert_key: str, target_status: str) -> None:
    with get_observability_connection(read_only=True) as conn:
        row = conn.execute(
            """
            SELECT alert_type, severity, dataset_name, symbol, message
            FROM obs_alerts
            WHERE alert_key = ?
            """,
            [alert_key],
        ).fetchone()
    if row is None:
        raise ValueError(f"Alert key not found: {alert_key}")
    alert_type, severity, dataset_name, symbol, message = row
    _emit_alert_state_change(
        logger,
        alert_key=alert_key,
        alert_type=str(alert_type),
        severity=str(severity),
        status=target_status,
        message=message if isinstance(message, str) else f"Alert moved to {target_status}.",
        dataset_name=str(dataset_name) if dataset_name else None,
        symbol=str(symbol) if symbol else None,
        manual_transition=True,
    )


def evaluate_alerts(*, trigger_type: str = "manual") -> dict[str, int]:
    initialize_observability()
    current_status = _current_alert_status()
    run_id = create_run_id()
    logger = BQuantLogger(
        PIPELINE_NAME,
        component="alerts",
        subcomponent=PIPELINE_NAME,
        default_channel="alerts",
    ).with_run_context(run_id=run_id, trigger_type=trigger_type)
    started_at = datetime.now()
    steps_completed: list[str] = []
    try:
        _evaluate_worker_heartbeat(logger, current_status)
        steps_completed.append("worker_heartbeat")
        _evaluate_intraday_lag(logger, current_status)
        steps_completed.append("intraday_lag")
        _evaluate_daily_eod(logger, current_status)
        steps_completed.append("daily_eod")
        _evaluate_consecutive_failures(logger, current_status)
        steps_completed.append("consecutive_failures")
        _evaluate_manifest_mismatch(logger, current_status)
        steps_completed.append("manifest_mismatch")
        _evaluate_pending_merge(logger, current_status)
        steps_completed.append("pending_merge")
        _evaluate_empty_payloads(logger, current_status)
        steps_completed.append("empty_payloads")

        finished_at = datetime.now()
        record_pipeline_run(
            run_id=run_id,
            pipeline_name=PIPELINE_NAME,
            start_time=started_at,
            end_time=finished_at,
            status="success",
            input_rows=0,
            output_rows=0,
        )
        logger.log_pipeline_run(
            pipeline_name=PIPELINE_NAME,
            start_time=started_at.isoformat(),
            end_time=finished_at.isoformat(),
            status="success",
            steps_completed=steps_completed,
            steps_failed=[],
            run_id=run_id,
            trigger_type=trigger_type,
        )
        return {"run_id": 1, "alerts_evaluated": len(steps_completed)}
    except Exception as exc:
        finished_at = datetime.now()
        record_pipeline_run(
            run_id=run_id,
            pipeline_name=PIPELINE_NAME,
            start_time=started_at,
            end_time=finished_at,
            status="failed",
            input_rows=0,
            output_rows=0,
            error_message=str(exc),
        )
        logger.log_error(
            PIPELINE_NAME,
            type(exc).__name__,
            str(exc),
            context={"run_id": run_id, "trigger_type": trigger_type, "steps_completed": steps_completed},
            channel="alerts",
        )
        logger.log_pipeline_run(
            pipeline_name=PIPELINE_NAME,
            start_time=started_at.isoformat(),
            end_time=finished_at.isoformat(),
            status="failed",
            steps_completed=steps_completed,
            steps_failed=["evaluate_alerts"],
            error_message=str(exc),
            run_id=run_id,
            trigger_type=trigger_type,
        )
        raise


def main() -> None:
    args = parse_args()
    initialize_observability()
    ingest_observability_logs()
    run_id = create_run_id()
    logger = BQuantLogger(
        PIPELINE_NAME,
        component="alerts",
        subcomponent=PIPELINE_NAME,
        default_channel="alerts",
    ).with_run_context(run_id=run_id, trigger_type=args.trigger_type)
    if args.ack:
        _manual_transition(logger, args.ack, "acknowledged")
        ingest_observability_logs(channel="alerts")
        return
    if args.resolve:
        _manual_transition(logger, args.resolve, "resolved")
        ingest_observability_logs(channel="alerts")
        return
    evaluate_alerts(trigger_type=args.trigger_type)
    ingest_observability_logs(channel="alerts")


if __name__ == "__main__":
    main()
