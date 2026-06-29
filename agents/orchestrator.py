"""Orchestrate BQuant deterministic agent recommendation cycles."""

from __future__ import annotations

import time
from datetime import date, datetime
from typing import Any

import pandas as pd

from agents.config import load_agent_config, optimizer_config
from agents.context_store import load_agent_context
from agents.deterministic_agents import (
    AGENT_VERSION,
    apply_portfolio_overlay,
    decisions_to_frame,
    score_context_row,
)
from agents.recommendation_store import record_agent_run, write_recommendations
from pipelines.live_update_runtime import create_run_id, record_pipeline_run
from utils.logger import BQuantLogger
from warehouse.refresh_state import bump_refresh_version, ensure_refresh_state_table


PIPELINE_NAME = "run_agent_cycle"
DATASET_NAME = "agent_recommendations"


def _context_as_of_date(frame: pd.DataFrame) -> date:
    """Resolve the single trading date represented by a context frame.

    Args:
        frame: Context rows loaded from `mart_agent_context_daily`.

    Returns:
        Trading date shared by the frame.
    """
    return pd.Timestamp(frame["trading_date"].max()).date()


def run_agent_cycle(
    *,
    as_of_date: date | str | None = None,
    trigger_type: str = "manual",
    limit: int | None = None,
) -> dict[str, Any]:
    """Run the deterministic BQuant agent recommendation cycle.

    Args:
        as_of_date: Optional trading date. Defaults to the latest dbt context
            mart date.
        trigger_type: `manual`, `scheduled`, or `recovery`.
        limit: Optional symbol row limit for smoke tests.

    Returns:
        Summary dictionary with run id, status, date, and output count.

    Raises:
        Exception: Re-raises failures after structured logging and run records.
    """
    ensure_refresh_state_table()
    config = load_agent_config()
    run_id = create_run_id()
    logger = BQuantLogger(
        PIPELINE_NAME,
        component="agent",
        subcomponent=PIPELINE_NAME,
        default_channel="pipeline",
    ).with_run_context(run_id=run_id, trigger_type=trigger_type, dataset_name=DATASET_NAME)
    start_time = datetime.now()
    started = time.perf_counter()
    input_rows = 0
    output_rows = 0
    steps_completed: list[str] = []
    steps_failed: list[str] = []
    optimizer_cfg = optimizer_config(config)
    optimizer_mode = str(optimizer_cfg.get("mode", "classical_fallback"))
    qaoa_enabled = bool(optimizer_cfg.get("qaoa_enabled", False))

    try:
        logger.info(
            "Starting BQuant agent cycle",
            event_type="job_start",
            status="running",
            as_of_date=str(as_of_date) if as_of_date else None,
            limit=limit,
            optimizer_mode=optimizer_mode,
            qaoa_enabled=qaoa_enabled,
        )
        context = load_agent_context(as_of_date=as_of_date, limit=limit, config=config)
        input_rows = int(len(context))
        target_date = _context_as_of_date(context)
        steps_completed.append("load_agent_context")

        decisions = [score_context_row(row, config) for _, row in context.iterrows()]
        decisions = apply_portfolio_overlay(decisions, config)
        recommendations = decisions_to_frame(decisions, run_id=run_id, as_of_date=target_date)
        steps_completed.append("score_recommendations")

        output_rows = write_recommendations(recommendations)
        steps_completed.append("write_recommendations")
        record_agent_run(
            run_id=run_id,
            as_of_date=target_date,
            trigger_type=trigger_type,
            status="success",
            input_rows=input_rows,
            output_rows=output_rows,
            agent_version=AGENT_VERSION,
            optimizer_mode=optimizer_mode,
            qaoa_enabled=qaoa_enabled,
        )
        steps_completed.append("record_agent_run")

        refresh_state = bump_refresh_version(
            DATASET_NAME,
            run_id=run_id,
            latest_data_ts=datetime.combine(target_date, datetime.min.time()),
            last_success_at=datetime.now(),
        )
        steps_completed.append("refresh_version")

        finished_at = datetime.now()
        record_pipeline_run(
            run_id=run_id,
            pipeline_name=PIPELINE_NAME,
            start_time=start_time,
            end_time=finished_at,
            status="success",
            input_rows=input_rows,
            output_rows=output_rows,
        )
        logger.log_pipeline_run(
            pipeline_name=PIPELINE_NAME,
            start_time=start_time.isoformat(),
            end_time=finished_at.isoformat(),
            status="success",
            steps_completed=steps_completed,
            steps_failed=steps_failed,
            run_id=run_id,
            trigger_type=trigger_type,
            dataset_name=DATASET_NAME,
            as_of_date=target_date.isoformat(),
            input_rows=input_rows,
            output_rows=output_rows,
            duration_seconds=round(time.perf_counter() - started, 3),
            refresh_version=refresh_state["refresh_version"],
            optimizer_mode=optimizer_mode,
            qaoa_enabled=qaoa_enabled,
        )
        return {
            "run_id": run_id,
            "status": "success",
            "as_of_date": target_date.isoformat(),
            "input_rows": input_rows,
            "output_rows": output_rows,
            "refresh_version": refresh_state["refresh_version"],
        }
    except Exception as exc:
        finished_at = datetime.now()
        steps_failed.append("run_agent_cycle")
        record_pipeline_run(
            run_id=run_id,
            pipeline_name=PIPELINE_NAME,
            start_time=start_time,
            end_time=finished_at,
            status="failed",
            input_rows=input_rows,
            output_rows=output_rows,
            error_message=str(exc),
        )
        try:
            record_agent_run(
                run_id=run_id,
                as_of_date=pd.Timestamp(as_of_date).date() if as_of_date else date.today(),
                trigger_type=trigger_type,
                status="failed",
                input_rows=input_rows,
                output_rows=output_rows,
                agent_version=AGENT_VERSION,
                optimizer_mode=optimizer_mode,
                qaoa_enabled=qaoa_enabled,
                notes=str(exc),
            )
        except Exception:
            pass
        logger.log_error(
            PIPELINE_NAME,
            type(exc).__name__,
            str(exc),
            context={
                "run_id": run_id,
                "trigger_type": trigger_type,
                "dataset_name": DATASET_NAME,
                "as_of_date": str(as_of_date) if as_of_date else None,
                "steps_completed": steps_completed,
                "steps_failed": steps_failed,
                "input_rows": input_rows,
                "output_rows": output_rows,
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
            input_rows=input_rows,
            output_rows=output_rows,
            duration_seconds=round(time.perf_counter() - started, 3),
        )
        raise
