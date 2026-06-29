"""One-shot data freshness updater for BQuant web/manual controls."""

from __future__ import annotations

import argparse
import time
from datetime import date, datetime, timedelta
from typing import Any

import pandas as pd

from agents.system_analysis import expected_daily_date
from pipelines.live_update_runtime import (
    create_run_id,
    get_latest_plot_timestamp,
    is_trading_day,
    latest_eligible_intraday_slot,
    now_local,
    record_pipeline_run,
)
from pipelines.run_eod_reconcile import run_eod_reconcile
from pipelines.run_intraday_delta import run_intraday_delta
from utils.logger import BQuantLogger
from warehouse.refresh_state import ensure_refresh_state_table


PIPELINE_NAME = "auto_update_data"
DATASET_NAME = "market_data_auto_update"


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments for the auto-update controller.

    Returns:
        Namespace with trigger metadata, dry-run flag, and max EOD catch-up
        safety limit.
    """
    parser = argparse.ArgumentParser(description="Update BQuant market data when it is stale.")
    parser.add_argument("--trigger-type", default="manual", choices=["manual", "scheduled", "recovery"])
    parser.add_argument("--dry-run", action="store_true", help="Resolve required jobs but do not run them.")
    parser.add_argument(
        "--max-eod-dates",
        type=int,
        default=5,
        help="Maximum missing daily trading dates to reconcile from this control-plane action.",
    )
    parser.add_argument(
        "--skip-system-analysis",
        action="store_true",
        help="Do not run the system-analysis agents after data jobs finish.",
    )
    return parser.parse_args()


def _date_from_timestamp(value: datetime | date | None) -> date | None:
    """Normalize a date/timestamp value to a Python date.

    Args:
        value: Timestamp-like value from DuckDB helpers.

    Returns:
        Date value, or `None`.
    """
    if value is None:
        return None
    return pd.Timestamp(value).date()


def _coverage_floor_date(table_name: str, date_column: str) -> date | None:
    """Return the oldest per-symbol latest date for a daily dataset.

    Args:
        table_name: DuckDB table to inspect.
        date_column: Date column used for coverage, such as `trading_date`.

    Returns:
        Minimum of per-symbol max dates, or `None` when the table is empty.
    """
    from warehouse.duckdb_connection import get_connection

    with get_connection(read_only=True) as conn:
        value = conn.execute(
            f"""
            SELECT min(latest_date)
            FROM (
                SELECT symbol, max({date_column}) AS latest_date
                FROM {table_name}
                GROUP BY symbol
            )
            """
        ).fetchone()[0]
    return _date_from_timestamp(value)


def _missing_trading_dates(latest_date: date | None, expected_date: date) -> list[date]:
    """List missing configured trading dates after a latest known date.

    Args:
        latest_date: Latest stored daily date across required daily datasets.
        expected_date: Expected latest daily date for current market time.

    Returns:
        Ordered list of trading dates that should be reconciled.
    """
    if latest_date is None or latest_date >= expected_date:
        return []
    missing: list[date] = []
    cursor = latest_date + timedelta(days=1)
    while cursor <= expected_date:
        if is_trading_day(cursor):
            missing.append(cursor)
        cursor += timedelta(days=1)
    return missing


def resolve_update_plan(*, max_eod_dates: int) -> dict[str, Any]:
    """Resolve which daily/intraday update jobs are currently needed.

    Args:
        max_eod_dates: Safety cap for web/manual daily catch-up runs.

    Returns:
        Plan dictionary containing missing EOD dates and optional intraday slot.

    Raises:
        RuntimeError: If a large daily gap indicates a controlled backfill is
            required instead of a one-click update.
    """
    expected_date = expected_daily_date(now_local())
    daily_floor = _coverage_floor_date("daily_ohlcv_base", "trading_date")
    index_floor = _coverage_floor_date("market_index_daily_base", "trading_date")
    latest_candidates = [value for value in [daily_floor, index_floor] if value is not None]
    if not latest_candidates:
        raise RuntimeError("No daily/index base data is available; run the controlled backfill pipeline first.")

    daily_anchor = min(latest_candidates)
    eod_dates = _missing_trading_dates(daily_anchor, expected_date)
    if len(eod_dates) > max_eod_dates:
        raise RuntimeError(
            f"Daily data is missing {len(eod_dates)} trading dates. "
            "Use the controlled backfill/rebuild pipeline instead of the web auto-update button."
        )

    eligible_slot = latest_eligible_intraday_slot(now_local())
    latest_intraday = get_latest_plot_timestamp()
    intraday_slot = None
    if eligible_slot is not None:
        latest_intraday_naive = latest_intraday.replace(tzinfo=None) if latest_intraday else None
        eligible_naive = eligible_slot.replace(tzinfo=None)
        if latest_intraday_naive is None or latest_intraday_naive < eligible_naive:
            intraday_slot = eligible_slot

    return {
        "expected_daily_date": expected_date,
        "daily_latest": daily_floor,
        "index_latest": index_floor,
        "missing_eod_dates": eod_dates,
        "latest_intraday": latest_intraday,
        "eligible_intraday_slot": eligible_slot,
        "intraday_slot_to_run": intraday_slot,
    }


def run_auto_update_data(
    *,
    trigger_type: str = "manual",
    dry_run: bool = False,
    max_eod_dates: int = 5,
    run_system_report: bool = True,
) -> dict[str, Any]:
    """Run required freshness jobs for daily and intraday market data.

    Args:
        trigger_type: Manual/scheduled/recovery trigger metadata.
        dry_run: If true, only return the resolved plan.
        max_eod_dates: Safety cap for missing daily dates.
        run_system_report: Whether to run system-analysis agents after data
            jobs finish.

    Returns:
        Summary dictionary with planned and executed jobs.

    Raises:
        Exception: Re-raises operational failures after structured logging.
    """
    ensure_refresh_state_table()
    run_id = create_run_id()
    logger = BQuantLogger(
        PIPELINE_NAME,
        component="pipeline",
        subcomponent=PIPELINE_NAME,
        default_channel="pipeline",
    ).with_run_context(run_id=run_id, trigger_type=trigger_type, dataset_name=DATASET_NAME)
    start_time = datetime.now()
    started = time.perf_counter()
    steps_completed: list[str] = []
    steps_failed: list[str] = []
    executed_jobs: list[dict[str, Any]] = []

    try:
        plan = resolve_update_plan(max_eod_dates=max_eod_dates)
        steps_completed.append("resolve_update_plan")
        logger.info(
            "Resolved BQuant auto-update plan",
            event_type="update_plan",
            status="planned",
            expected_daily_date=plan["expected_daily_date"],
            daily_latest=plan["daily_latest"],
            index_latest=plan["index_latest"],
            missing_eod_dates=plan["missing_eod_dates"],
            intraday_slot_to_run=plan["intraday_slot_to_run"],
            dry_run=dry_run,
        )

        if dry_run:
            finished_at = datetime.now()
            record_pipeline_run(
                run_id=run_id,
                pipeline_name=PIPELINE_NAME,
                start_time=start_time,
                end_time=finished_at,
                status="skipped",
                input_rows=0,
                output_rows=0,
                error_message="Dry-run mode: no update jobs executed.",
            )
            logger.log_pipeline_run(
                pipeline_name=PIPELINE_NAME,
                start_time=start_time.isoformat(),
                end_time=finished_at.isoformat(),
                status="skipped",
                steps_completed=steps_completed,
                steps_failed=steps_failed,
                run_id=run_id,
                trigger_type=trigger_type,
                dataset_name=DATASET_NAME,
                duration_seconds=round(time.perf_counter() - started, 3),
                plan=plan,
                dry_run=True,
            )
            return {"run_id": run_id, "status": "skipped", "plan": plan, "executed_jobs": []}

        for trade_date in plan["missing_eod_dates"]:
            result = run_eod_reconcile(
                trade_date=trade_date,
                trigger_type=trigger_type,
                run_post_hooks=False,
            )
            executed_jobs.append({"job": "run_eod_reconcile", "trade_date": trade_date.isoformat(), "result": result})
        if plan["missing_eod_dates"]:
            steps_completed.append("run_eod_reconcile")

        intraday_slot = plan["intraday_slot_to_run"]
        if intraday_slot is not None:
            result = run_intraday_delta(
                slot_dt=intraday_slot,
                trigger_type=trigger_type,
                run_post_hooks=False,
            )
            executed_jobs.append({"job": "run_intraday_delta", "slot_time": intraday_slot.isoformat(), "result": result})
            steps_completed.append("run_intraday_delta")

        if run_system_report:
            from agents.system_analysis import run_system_analysis

            analysis_result = run_system_analysis(
                trigger_type=trigger_type,
                refresh_recommendations=True,
            )
            executed_jobs.append({"job": "run_agent_system_analysis", "result": analysis_result})
            steps_completed.append("run_agent_system_analysis")

        finished_at = datetime.now()
        status = "success" if executed_jobs else "skipped"
        record_pipeline_run(
            run_id=run_id,
            pipeline_name=PIPELINE_NAME,
            start_time=start_time,
            end_time=finished_at,
            status=status,
            input_rows=0,
            output_rows=len(executed_jobs),
            error_message=None if executed_jobs else "Data is already fresh for the current market clock.",
        )
        logger.log_pipeline_run(
            pipeline_name=PIPELINE_NAME,
            start_time=start_time.isoformat(),
            end_time=finished_at.isoformat(),
            status=status,
            steps_completed=steps_completed,
            steps_failed=steps_failed,
            run_id=run_id,
            trigger_type=trigger_type,
            dataset_name=DATASET_NAME,
            duration_seconds=round(time.perf_counter() - started, 3),
            plan=plan,
            executed_jobs=executed_jobs,
        )
        return {"run_id": run_id, "status": status, "plan": plan, "executed_jobs": executed_jobs}
    except Exception as exc:
        steps_failed.append("auto_update_data")
        finished_at = datetime.now()
        record_pipeline_run(
            run_id=run_id,
            pipeline_name=PIPELINE_NAME,
            start_time=start_time,
            end_time=finished_at,
            status="failed",
            input_rows=0,
            output_rows=len(executed_jobs),
            error_message=str(exc),
        )
        logger.log_error(
            PIPELINE_NAME,
            type(exc).__name__,
            str(exc),
            context={
                "run_id": run_id,
                "trigger_type": trigger_type,
                "steps_completed": steps_completed,
                "steps_failed": steps_failed,
                "executed_jobs": executed_jobs,
            },
            channel="pipeline",
        )
        logger.log_pipeline_run(
            pipeline_name=PIPELINE_NAME,
            start_time=start_time.isoformat(),
            end_time=finished_at.isoformat(),
            status="failed",
            steps_completed=steps_completed,
            steps_failed=steps_failed,
            error_message=str(exc),
            run_id=run_id,
            trigger_type=trigger_type,
            dataset_name=DATASET_NAME,
            duration_seconds=round(time.perf_counter() - started, 3),
        )
        raise


def main() -> dict[str, Any]:
    """Run the CLI auto-update controller."""
    args = parse_args()
    return run_auto_update_data(
        trigger_type=args.trigger_type,
        dry_run=args.dry_run,
        max_eod_dates=args.max_eod_dates,
        run_system_report=not args.skip_system_analysis,
    )


if __name__ == "__main__":
    main()
