"""Refresh materialized dataset files and manifest rows from current warehouse tables."""

from __future__ import annotations

import argparse
from datetime import datetime

from utils.logger import BQuantLogger
from warehouse.data_manifest import materialize_dataset_from_table
from warehouse.init_observability import initialize_observability

from pipelines.ingest_observability_logs import ingest_observability_logs
from pipelines.live_update_runtime import create_run_id, record_pipeline_run


PIPELINE_NAME = "refresh_manifest"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Refresh current materialized dataset files and manifest.")
    parser.add_argument("--trigger-type", default="manual", choices=["manual", "scheduled", "recovery"])
    return parser.parse_args()


def refresh_manifest(*, trigger_type: str = "manual") -> dict[str, int]:
    initialize_observability()
    run_id = create_run_id()
    logger = BQuantLogger(PIPELINE_NAME, component="pipeline", subcomponent=PIPELINE_NAME).with_run_context(
        run_id=run_id,
        trigger_type=trigger_type,
    )
    started_at = datetime.now()
    try:
        daily = materialize_dataset_from_table("daily_ohlcv_10y")
        intraday_base = materialize_dataset_from_table("intraday_ohlcv_15m_60d")
        intraday_delta = materialize_dataset_from_table("intraday_ohlcv_15m_delta")
        finished_at = datetime.now()
        record_pipeline_run(
            run_id=run_id,
            pipeline_name=PIPELINE_NAME,
            start_time=started_at,
            end_time=finished_at,
            status="success",
            input_rows=0,
            output_rows=len(daily) + len(intraday_base) + len(intraday_delta),
        )
        logger.log_pipeline_run(
            pipeline_name=PIPELINE_NAME,
            start_time=started_at.isoformat(),
            end_time=finished_at.isoformat(),
            status="success",
            steps_completed=["daily", "intraday_base", "intraday_delta"],
            steps_failed=[],
            run_id=run_id,
            trigger_type=trigger_type,
            output_rows=len(daily) + len(intraday_base) + len(intraday_delta),
        )
        ingest_observability_logs()
        return {
            "daily": len(daily),
            "intraday_base": len(intraday_base),
            "intraday_delta": len(intraday_delta),
        }
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
            context={"run_id": run_id, "trigger_type": trigger_type},
        )
        logger.log_pipeline_run(
            pipeline_name=PIPELINE_NAME,
            start_time=started_at.isoformat(),
            end_time=finished_at.isoformat(),
            status="failed",
            steps_completed=[],
            steps_failed=["refresh_manifest"],
            error_message=str(exc),
            run_id=run_id,
            trigger_type=trigger_type,
        )
        ingest_observability_logs()
        raise


def main() -> None:
    args = parse_args()
    refresh_manifest(trigger_type=args.trigger_type)


if __name__ == "__main__":
    main()
