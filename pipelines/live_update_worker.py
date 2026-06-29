"""Long-running local worker for BQuant live update scheduling."""

from __future__ import annotations

import argparse
import time
from datetime import datetime

from utils.logger import BQuantLogger
from warehouse.init_observability import initialize_observability

from pipelines.evaluate_alerts import evaluate_alerts
from pipelines.ingest_observability_logs import ingest_observability_logs
from pipelines.live_update_runtime import (
    create_run_id,
    eod_reconcile_time,
    get_latest_daily_date,
    get_plot_floor_timestamp,
    is_trading_day,
    load_live_update_config,
    market_session_state,
    now_local,
    recent_due_intraday_slots,
    record_pipeline_run,
)
from pipelines.run_eod_reconcile import run_eod_reconcile
from pipelines.run_intraday_delta import run_intraday_delta


PIPELINE_NAME = "live_update_worker"


def parse_args() -> argparse.Namespace:
    """Parse CLI flags for one-shot and dry-run worker execution."""
    parser = argparse.ArgumentParser(description="Run the BQuant live update worker.")
    parser.add_argument("--once", action="store_true", help="Run a single scheduling iteration and exit.")
    parser.add_argument("--dry-run", action="store_true", help="Evaluate scheduling decisions without dispatching jobs.")
    return parser.parse_args()


def _delta_rows_pending(trade_date) -> bool:
    """Return whether delta rows still exist for the supplied trade date."""
    from warehouse.duckdb_connection import get_connection

    with get_connection(read_only=True) as conn:
        count = conn.execute(
            "SELECT count(*) FROM intraday_ohlcv_15m_delta WHERE session_date <= ?",
            [trade_date],
        ).fetchone()[0]
    return bool(count)


def main() -> None:
    """Run the scheduler loop that dispatches intraday, EOD, ingest, and alert jobs."""
    args = parse_args()
    initialize_observability()
    cfg = load_live_update_config()
    worker_cfg = cfg.get("worker", {})
    worker_name = str(worker_cfg.get("name", "bquant_live_worker"))
    poll_interval = int(worker_cfg.get("poll_interval_seconds", 60))
    ingest_interval = int(worker_cfg.get("ingest_logs_every_seconds", 60))
    alert_interval = int(worker_cfg.get("evaluate_alerts_every_seconds", 60))
    failure_escalation = int(worker_cfg.get("consecutive_failure_escalation", 2))
    slot_retry_interval = int(worker_cfg.get("slot_retry_interval_seconds", 300))
    eod_retry_interval = int(worker_cfg.get("eod_retry_interval_seconds", 1800))

    logger = BQuantLogger(
        PIPELINE_NAME,
        component="scheduler",
        subcomponent=worker_name,
        default_channel="scheduler",
    )
    run_id = create_run_id()
    started_at = datetime.now()
    consecutive_failures = 0
    last_ingest_at = 0.0
    last_alert_eval_at = 0.0
    last_iteration_status = "success"
    last_error_message: str | None = None
    slot_attempted_at: dict[str, float] = {}
    eod_attempted_at: dict[str, float] = {}

    logger.log_scheduler_event(
        "Starting live update worker",
        event_type="scheduler_startup",
        status="running",
        run_id=run_id,
        trigger_type="scheduled",
        worker_name=worker_name,
    )

    try:
        while True:
            loop_started = time.time()
            current = now_local()
            session_state = market_session_state(current)
            logger.log_scheduler_event(
                "Worker heartbeat",
                event_type="scheduler_heartbeat",
                status="alive",
                run_id=run_id,
                trigger_type="scheduled",
                worker_name=worker_name,
                session_state=session_state,
                current_time=current.isoformat(),
            )

            try:
                last_iteration_status = "success"
                last_error_message = None
                if is_trading_day(current):
                    # Dispatch each due slot independently so recovery slots can be replayed
                    # without re-running already-covered intervals.
                    floor_ts = get_plot_floor_timestamp()
                    due_slots = recent_due_intraday_slots(current)
                    latest_due_slot = due_slots[-1] if due_slots else None
                    for slot_dt in due_slots:
                        slot_naive = slot_dt.replace(tzinfo=None)
                        if floor_ts is not None and floor_ts >= slot_naive:
                            logger.log_scheduler_event(
                                "Skipping already-covered intraday slot",
                                event_type="duplicate_slot_suppressed",
                                status="skipped",
                                run_id=run_id,
                                trigger_type="scheduled",
                                worker_name=worker_name,
                                slot_time=slot_dt.isoformat(),
                                floor_timestamp=floor_ts.isoformat(),
                            )
                            continue
                        trigger_type = "recovery" if latest_due_slot is not None and slot_dt != latest_due_slot else "scheduled"
                        slot_key = slot_dt.isoformat()
                        now_ts = time.time()
                        seconds_since_attempt = now_ts - slot_attempted_at.get(slot_key, 0.0)
                        if seconds_since_attempt < slot_retry_interval:
                            logger.log_scheduler_event(
                                "Skipping recently-attempted intraday slot",
                                event_type="slot_retry_suppressed",
                                status="skipped",
                                run_id=run_id,
                                trigger_type=trigger_type,
                                worker_name=worker_name,
                                slot_time=slot_key,
                                retry_after_seconds=round(slot_retry_interval - seconds_since_attempt, 1),
                            )
                            continue
                        logger.log_scheduler_event(
                            "Dispatching intraday delta job",
                            event_type="due_slot_detected",
                            status="running",
                            run_id=run_id,
                            trigger_type=trigger_type,
                            worker_name=worker_name,
                            slot_time=slot_dt.isoformat(),
                        )
                        if not args.dry_run:
                            run_intraday_delta(slot_dt=slot_dt, trigger_type=trigger_type, run_post_hooks=True)
                            slot_attempted_at[slot_key] = now_ts
                        else:
                            logger.log_scheduler_event(
                                "Dry-run mode: skipped intraday delta dispatch",
                                event_type="dry_run_skip_dispatch",
                                status="skipped",
                                run_id=run_id,
                                trigger_type=trigger_type,
                                worker_name=worker_name,
                                slot_time=slot_dt.isoformat(),
                            )
                            slot_attempted_at[slot_key] = now_ts
                        floor_ts = get_plot_floor_timestamp()

                    # After the EOD buffer time, reconcile if the daily row is stale or
                    # intraday delta data still needs to be merged into the base dataset.
                    if current >= eod_reconcile_time(current.date()):
                        latest_daily = get_latest_daily_date()
                        if latest_daily is None or latest_daily < current.date() or _delta_rows_pending(current.date()):
                            eod_key = current.date().isoformat()
                            now_ts = time.time()
                            seconds_since_attempt = now_ts - eod_attempted_at.get(eod_key, 0.0)
                            if seconds_since_attempt < eod_retry_interval:
                                logger.log_scheduler_event(
                                    "Skipping recently-attempted EOD reconcile",
                                    event_type="eod_retry_suppressed",
                                    status="skipped",
                                    run_id=run_id,
                                    trigger_type="scheduled",
                                    worker_name=worker_name,
                                    trade_date=eod_key,
                                    retry_after_seconds=round(eod_retry_interval - seconds_since_attempt, 1),
                                )
                            else:
                                logger.log_scheduler_event(
                                    "Dispatching EOD reconcile job",
                                    event_type="eod_dispatch",
                                    status="running",
                                    run_id=run_id,
                                    trigger_type="scheduled",
                                    worker_name=worker_name,
                                    trade_date=current.date().isoformat(),
                                )
                                eod_attempted_at[eod_key] = now_ts
                                if not args.dry_run:
                                    run_eod_reconcile(
                                        trade_date=current.date(),
                                        trigger_type="scheduled",
                                        run_post_hooks=True,
                                    )
                                else:
                                    logger.log_scheduler_event(
                                        "Dry-run mode: skipped EOD reconcile dispatch",
                                        event_type="dry_run_skip_dispatch",
                                        status="skipped",
                                        run_id=run_id,
                                        trigger_type="scheduled",
                                        worker_name=worker_name,
                                        trade_date=current.date().isoformat(),
                                    )
                else:
                    logger.log_scheduler_event(
                        "Market is closed; worker is idle",
                        event_type="market_closed",
                        status="skipped",
                        run_id=run_id,
                        trigger_type="scheduled",
                        worker_name=worker_name,
                        current_time=current.isoformat(),
                    )

                now_ts = time.time()
                if now_ts - last_ingest_at >= ingest_interval:
                    ingest_observability_logs()
                    last_ingest_at = now_ts
                if now_ts - last_alert_eval_at >= alert_interval:
                    evaluate_alerts(trigger_type="scheduled")
                    ingest_observability_logs(channel="alerts")
                    last_alert_eval_at = now_ts
                consecutive_failures = 0
            except Exception as exc:
                consecutive_failures += 1
                last_iteration_status = "failed"
                last_error_message = str(exc)
                logger.log_error(
                    PIPELINE_NAME,
                    type(exc).__name__,
                    str(exc),
                    context={
                        "run_id": run_id,
                        "trigger_type": "scheduled",
                        "worker_name": worker_name,
                        "session_state": session_state,
                        "consecutive_failures": consecutive_failures,
                    },
                    channel="scheduler",
                )
                if consecutive_failures >= failure_escalation:
                    logger.warning(
                        f"Worker consecutive failures reached {consecutive_failures}",
                        event_type="consecutive_failure_escalation",
                        status="warning",
                        channel="scheduler",
                        worker_name=worker_name,
                        run_id=run_id,
                        trigger_type="scheduled",
                        consecutive_failures=consecutive_failures,
                    )

            elapsed = time.time() - loop_started
            if args.once:
                ended_at = datetime.now()
                record_pipeline_run(
                    run_id=run_id,
                    pipeline_name=PIPELINE_NAME,
                    start_time=started_at,
                    end_time=ended_at,
                    status=last_iteration_status,
                    input_rows=0,
                    output_rows=0,
                    error_message=last_error_message,
                )
                logger.log_pipeline_run(
                    pipeline_name=PIPELINE_NAME,
                    start_time=started_at.isoformat(),
                    end_time=ended_at.isoformat(),
                    status=last_iteration_status,
                    steps_completed=["worker_iteration"] if last_iteration_status == "success" else [],
                    steps_failed=[] if last_iteration_status == "success" else ["worker_iteration"],
                    run_id=run_id,
                    trigger_type="scheduled",
                    worker_name=worker_name,
                    dry_run=args.dry_run,
                    error_message=last_error_message,
                )
                break
            time.sleep(max(poll_interval - elapsed, 0))
    except KeyboardInterrupt:
        ended_at = datetime.now()
        record_pipeline_run(
            run_id=run_id,
            pipeline_name=PIPELINE_NAME,
            start_time=started_at,
            end_time=ended_at,
            status="success",
            input_rows=0,
            output_rows=0,
        )
        logger.log_pipeline_run(
            pipeline_name=PIPELINE_NAME,
            start_time=started_at.isoformat(),
            end_time=ended_at.isoformat(),
            status="success",
            steps_completed=["worker_loop"],
            steps_failed=[],
            run_id=run_id,
            trigger_type="scheduled",
            worker_name=worker_name,
        )
        logger.log_scheduler_event(
            "Shutting down live update worker",
            event_type="scheduler_shutdown",
            status="stopped",
            run_id=run_id,
            trigger_type="scheduled",
            worker_name=worker_name,
        )


if __name__ == "__main__":
    main()
