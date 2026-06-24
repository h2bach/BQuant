"""Run the live 15-minute intraday delta update job."""

from __future__ import annotations

import argparse
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from typing import Any

import pandas as pd

from data_ingestion.fetch_intraday_15m import upsert_intraday_rows
from data_ingestion.yfinance_adapter import fetch_intraday_history, source_config
from utils.logger import BQuantLogger
from utils.rate_limit import SlidingWindowRateLimiter
from warehouse.data_manifest import materialize_dataset_from_table
from warehouse.init_observability import initialize_observability
from warehouse.observability_connection import get_observability_connection
from warehouse.refresh_state import bump_refresh_version, ensure_refresh_state_table

from pipelines.evaluate_alerts import evaluate_alerts
from pipelines.ingest_observability_logs import ingest_observability_logs
from pipelines.live_update_runtime import (
    create_run_id,
    get_latest_plot_timestamp,
    intraday_overlap_delta,
    latest_eligible_intraday_slot,
    load_live_update_config,
    market_timezone,
    now_local,
    record_pipeline_run,
    resolve_universe_symbols,
)


PIPELINE_NAME = "run_intraday_delta"
DATASET_NAME = "intraday_ohlcv_15m_delta"


def parse_args() -> argparse.Namespace:
    """Parse CLI arguments for the live intraday delta updater."""
    parser = argparse.ArgumentParser(description="Run the live intraday delta updater.")
    parser.add_argument("--slot-time", help="Target slot time in ISO format, e.g. 2026-06-24T10:15:00.")
    parser.add_argument("--symbols", nargs="*", help="Optional symbol subset.")
    parser.add_argument("--test", action="store_true", help="Use test universe only.")
    parser.add_argument("--trigger-type", default="manual", choices=["manual", "scheduled", "recovery"])
    parser.add_argument("--skip-post-hooks", action="store_true", help="Skip observability ingest and alert evaluation.")
    parser.add_argument("--dry-run", action="store_true", help="Resolve slot and symbols, but do not fetch or write data.")
    return parser.parse_args()


def _slot_datetime(slot_time: str | None) -> datetime | None:
    """Resolve the target slot datetime from CLI input or the latest eligible slot."""
    if slot_time:
        slot_dt = datetime.fromisoformat(slot_time)
        if slot_dt.tzinfo is None:
            slot_dt = slot_dt.replace(tzinfo=market_timezone())
        return slot_dt
    return latest_eligible_intraday_slot(now_local())


def _checkpoint_watermark(symbol: str) -> datetime | None:
    """Resolve the current symbol watermark from observability or plot data state."""
    initialize_observability()
    with get_observability_connection(read_only=True) as conn:
        value = conn.execute(
            """
            SELECT watermark_ts
            FROM obs_live_checkpoints
            WHERE dataset_name = ?
              AND symbol = ?
            """,
            [DATASET_NAME, symbol],
        ).fetchone()
    if value and value[0] is not None:
        ts = pd.Timestamp(value[0]).to_pydatetime()
        return ts.replace(tzinfo=market_timezone()) if ts.tzinfo is None else ts.astimezone(market_timezone())
    latest_plot = get_latest_plot_timestamp(symbol)
    if latest_plot is None:
        return None
    return latest_plot.replace(tzinfo=market_timezone()) if latest_plot.tzinfo is None else latest_plot.astimezone(market_timezone())


def _fetch_symbol_delta(
    symbol: str,
    slot_dt: datetime,
    limiter: SlidingWindowRateLimiter,
    logger: BQuantLogger,
) -> tuple[str, pd.DataFrame, datetime | None]:
    """Fetch one symbol's overlapping delta slice for the requested intraday slot."""
    request_id = create_run_id()
    request_start = datetime.now()
    watermark = _checkpoint_watermark(symbol)
    # Fetch one overlapping bar to avoid dropping the edge of a slot during
    # retries, restarts, or provider-side late writes.
    overlap = intraday_overlap_delta()
    fetch_start = watermark - overlap if watermark else slot_dt - timedelta(days=2)
    waited = limiter.acquire()
    duration_seconds = 0.0

    try:
        if waited > 0:
            logger.emit_event(
                "Paused for source rate limit before intraday fetch",
                level=logging.INFO,
                channel="ingestion",
                event_type="rate_limit_wait",
                legacy_category="data",
                data_subcategory="ingestion",
                symbol=symbol,
                dataset_name=DATASET_NAME,
                wait_seconds=round(waited, 2),
            )
        chunk = fetch_intraday_history(
            symbol,
            fetch_start.strftime("%Y-%m-%d %H:%M:%S"),
            slot_dt.strftime("%Y-%m-%d %H:%M:%S"),
            interval="15m",
        )
        if not chunk.empty:
            chunk = chunk.loc[pd.to_datetime(chunk["bar_time"]) <= slot_dt.replace(tzinfo=None)].copy()
        duration_seconds = time.perf_counter()
        request_end = datetime.now()
        logger.emit_event(
            f"Completed intraday source request for {symbol}",
            level=logging.INFO,
            channel="ingestion",
            event_type="source_request",
            status="empty" if chunk.empty else "success",
            legacy_category="data",
            data_subcategory="ingestion",
            dataset_name=DATASET_NAME,
            symbol=symbol,
            request_id=request_id,
            provider="yfinance",
            request_start=request_start.isoformat(),
            request_end=request_end.isoformat(),
            duration_seconds=round((request_end - request_start).total_seconds(), 3),
            rows_fetched=int(len(chunk)),
            wait_seconds=round(waited, 3),
            empty_payload=bool(chunk.empty),
            watermark_ts=watermark.isoformat() if watermark else None,
            requested_slot=slot_dt.isoformat(),
            fetch_start=fetch_start.isoformat(),
        )
        return symbol, chunk, watermark
    except Exception as exc:
        request_end = datetime.now()
        logger.emit_event(
            f"Intraday source request failed for {symbol}",
            level=logging.ERROR,
            channel="ingestion",
            event_type="source_request",
            status="failed",
            legacy_category="error",
            dataset_name=DATASET_NAME,
            symbol=symbol,
            request_id=request_id,
            provider="yfinance",
            request_start=request_start.isoformat(),
            request_end=request_end.isoformat(),
            duration_seconds=round((request_end - request_start).total_seconds(), 3),
            rows_fetched=0,
            wait_seconds=round(waited, 3),
            empty_payload=False,
            watermark_ts=watermark.isoformat() if watermark else None,
            requested_slot=slot_dt.isoformat(),
            fetch_start=fetch_start.isoformat(),
            error_type=type(exc).__name__,
            error_message=str(exc),
        )
        return symbol, pd.DataFrame(), watermark


def run_intraday_delta(
    *,
    slot_dt: datetime | None = None,
    symbols: list[str] | None = None,
    use_test_symbols: bool = False,
    trigger_type: str = "manual",
    run_post_hooks: bool = True,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Run the intraday delta fetch, upsert, materialization, and post-hook flow."""
    initialize_observability()
    ensure_refresh_state_table()

    cfg = load_live_update_config()
    run_id = create_run_id()
    job_logger = BQuantLogger(
        PIPELINE_NAME,
        component="pipeline",
        subcomponent=PIPELINE_NAME,
        default_channel="pipeline",
    ).with_run_context(run_id=run_id, trigger_type=trigger_type, dataset_name=DATASET_NAME)

    slot_dt = slot_dt or latest_eligible_intraday_slot(now_local())
    start_time = datetime.now()
    perf_started = time.perf_counter()
    steps_completed: list[str] = []
    steps_failed: list[str] = []
    total_fetched_rows = 0
    total_saved_rows = 0
    target_symbols = resolve_universe_symbols(explicit_symbols=symbols, use_test=use_test_symbols)

    if slot_dt is None:
        end_time = datetime.now()
        record_pipeline_run(
            run_id=run_id,
            pipeline_name=PIPELINE_NAME,
            start_time=start_time,
            end_time=end_time,
            status="skipped",
            input_rows=0,
            output_rows=0,
            error_message="No eligible intraday slot at current local time.",
        )
        job_logger.log_pipeline_run(
            pipeline_name=PIPELINE_NAME,
            start_time=start_time.isoformat(),
            end_time=end_time.isoformat(),
            status="skipped",
            steps_completed=[],
            steps_failed=["resolve_slot"],
            error_message="No eligible intraday slot at current local time.",
            run_id=run_id,
            trigger_type=trigger_type,
            dataset_name=DATASET_NAME,
        )
        return {"run_id": run_id, "status": "skipped", "slot_time": None}

    source_cfg = source_config()
    rpm = int(source_cfg.get("rate_limit", {}).get("requests_per_minute", 120))
    max_workers = max(int(cfg.get("jobs", {}).get("intraday_delta", {}).get("max_parallel_symbols", 2)), 1)
    limiter = SlidingWindowRateLimiter(rpm)

    try:
        job_logger.info(
            "Starting live intraday delta update",
            event_type="job_start",
            status="running",
            dataset_name=DATASET_NAME,
            slot_time=slot_dt.isoformat(),
            symbols=target_symbols,
            max_workers=max_workers,
            requests_per_minute=rpm,
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
                error_message="Dry-run mode: no source fetch or writes executed.",
            )
            job_logger.log_pipeline_run(
                pipeline_name=PIPELINE_NAME,
                start_time=start_time.isoformat(),
                end_time=finished_at.isoformat(),
                status="skipped",
                steps_completed=["resolve_slot", "resolve_symbols"],
                steps_failed=[],
                run_id=run_id,
                trigger_type=trigger_type,
                dataset_name=DATASET_NAME,
                duration_seconds=round(time.perf_counter() - perf_started, 3),
                slot_time=slot_dt.isoformat(),
                dry_run=True,
            )
            return {
                "run_id": run_id,
                "status": "skipped",
                "slot_time": slot_dt.isoformat(),
                "rows_fetched": 0,
                "rows_saved": 0,
                "dry_run": True,
            }

        frames: list[pd.DataFrame] = []
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(_fetch_symbol_delta, symbol, slot_dt, limiter, job_logger): symbol
                for symbol in target_symbols
            }
            for future in as_completed(futures):
                symbol, chunk, _ = future.result()
                total_fetched_rows += int(len(chunk))
                if not chunk.empty:
                    chunk = chunk.copy()
                    chunk["snapshot_date"] = slot_dt.date()
                    frames.append(chunk)
        steps_completed.append("fetch_intraday_source")

        combined_df = (
            pd.concat(frames, ignore_index=True).sort_values(["symbol", "bar_time"]).reset_index(drop=True)
            if frames
            else pd.DataFrame()
        )
        steps_completed.append("combine_rows")

        non_empty_symbols = sorted({str(symbol) for symbol in combined_df["symbol"].unique()}) if not combined_df.empty else []
        if combined_df.empty:
            finished_at = datetime.now()
            record_pipeline_run(
                run_id=run_id,
                pipeline_name=PIPELINE_NAME,
                start_time=start_time,
                end_time=finished_at,
                status="no_data",
                input_rows=0,
                output_rows=0,
                error_message="Source returned no intraday rows for the requested slot.",
            )
            job_logger.emit_event(
                "No intraday rows returned for the requested slot",
                level=logging.WARNING,
                channel="pipeline",
                event_type="no_data",
                status="no_data",
                dataset_name=DATASET_NAME,
                slot_time=slot_dt.isoformat(),
                symbols=target_symbols,
            )
            job_logger.log_pipeline_run(
                pipeline_name=PIPELINE_NAME,
                start_time=start_time.isoformat(),
                end_time=finished_at.isoformat(),
                status="no_data",
                steps_completed=steps_completed,
                steps_failed=[],
                error_message="Source returned no intraday rows for the requested slot.",
                run_id=run_id,
                trigger_type=trigger_type,
                dataset_name=DATASET_NAME,
                input_rows=0,
                output_rows=0,
                duration_seconds=round(time.perf_counter() - perf_started, 3),
                slot_time=slot_dt.isoformat(),
            )
            if run_post_hooks:
                ingest_observability_logs()
                evaluate_alerts(trigger_type=trigger_type)
                ingest_observability_logs(channel="alerts")
            return {
                "run_id": run_id,
                "status": "no_data",
                "slot_time": slot_dt.isoformat(),
                "rows_fetched": 0,
                "rows_saved": 0,
                "symbols_with_data": non_empty_symbols,
            }

        total_saved_rows = upsert_intraday_rows("intraday_ohlcv_15m_delta", combined_df)
        steps_completed.append("upsert_delta_table")

        materialized_files = materialize_dataset_from_table(
            DATASET_NAME,
            symbols=target_symbols,
            snapshot_date=slot_dt.date(),
        )
        steps_completed.append("materialize_delta_files")

        # Checkpoint events let the observability projection reconstruct which
        # symbol/bar the live worker considers covered after this run.
        for symbol in target_symbols:
            latest_symbol_ts = get_latest_plot_timestamp(symbol)
            job_logger.emit_event(
                f"Updated intraday checkpoint for {symbol}",
                level=logging.INFO,
                channel="pipeline",
                event_type="checkpoint_updated",
                status="success",
                dataset_name=DATASET_NAME,
                symbol=symbol,
                watermark_ts=latest_symbol_ts.isoformat() if latest_symbol_ts else None,
                requested_slot=slot_dt.isoformat(),
            )
        steps_completed.append("emit_checkpoints")

        refresh_state = None
        if total_saved_rows > 0:
            refresh_state = bump_refresh_version(
                DATASET_NAME,
                run_id=run_id,
                latest_data_ts=max(pd.to_datetime(combined_df["bar_time"])).to_pydatetime() if not combined_df.empty else None,
                last_success_at=datetime.now(),
            )
            job_logger.emit_event(
                "Bumped dataset refresh version after intraday delta update",
                level=logging.INFO,
                channel="pipeline",
                event_type="dataset_refresh",
                status="success",
                dataset_name=DATASET_NAME,
                refresh_version=refresh_state["refresh_version"],
                latest_data_ts=refresh_state["latest_data_ts"],
            )
        steps_completed.append("refresh_version")

        finished_at = datetime.now()
        record_pipeline_run(
            run_id=run_id,
            pipeline_name=PIPELINE_NAME,
            start_time=start_time,
            end_time=finished_at,
            status="success",
            input_rows=total_fetched_rows,
            output_rows=total_saved_rows,
        )
        steps_completed.append("record_pipeline_run")

        job_logger.log_data_event(
            "ingestion",
            "Intraday delta dataset refreshed",
            event_type="dataset_refresh",
            status="success",
            dataset_name=DATASET_NAME,
            slot_time=slot_dt.isoformat(),
            rows_fetched=total_fetched_rows,
            rows_saved=total_saved_rows,
            materialized_files=materialized_files,
        )
        job_logger.log_pipeline_run(
            pipeline_name=PIPELINE_NAME,
            start_time=start_time.isoformat(),
            end_time=finished_at.isoformat(),
            status="success",
            steps_completed=steps_completed,
            steps_failed=steps_failed,
            run_id=run_id,
            trigger_type=trigger_type,
            dataset_name=DATASET_NAME,
            input_rows=total_fetched_rows,
            output_rows=total_saved_rows,
            duration_seconds=round(time.perf_counter() - perf_started, 3),
            slot_time=slot_dt.isoformat(),
        )

        if run_post_hooks:
            ingest_observability_logs()
            evaluate_alerts(trigger_type=trigger_type)
            ingest_observability_logs(channel="alerts")

        return {
            "run_id": run_id,
            "status": "success",
            "slot_time": slot_dt.isoformat(),
            "rows_fetched": total_fetched_rows,
            "rows_saved": total_saved_rows,
        }
    except Exception as exc:
        finished_at = datetime.now()
        steps_failed.append("run_intraday_delta")
        record_pipeline_run(
            run_id=run_id,
            pipeline_name=PIPELINE_NAME,
            start_time=start_time,
            end_time=finished_at,
            status="failed",
            input_rows=total_fetched_rows,
            output_rows=total_saved_rows,
            error_message=str(exc),
        )
        job_logger.log_error(
            PIPELINE_NAME,
            type(exc).__name__,
            str(exc),
            context={
                "run_id": run_id,
                "trigger_type": trigger_type,
                "dataset_name": DATASET_NAME,
                "slot_time": slot_dt.isoformat() if slot_dt else None,
                "steps_completed": steps_completed,
                "steps_failed": steps_failed,
                "rows_fetched": total_fetched_rows,
                "rows_saved": total_saved_rows,
            },
            channel="pipeline",
        )
        job_logger.log_pipeline_run(
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
            input_rows=total_fetched_rows,
            output_rows=total_saved_rows,
            duration_seconds=round(time.perf_counter() - perf_started, 3),
            slot_time=slot_dt.isoformat() if slot_dt else None,
        )
        if run_post_hooks:
            ingest_observability_logs()
            evaluate_alerts(trigger_type=trigger_type)
            ingest_observability_logs(channel="alerts")
        raise


def main() -> None:
    """CLI entrypoint for the live intraday delta updater."""
    args = parse_args()
    slot_dt = _slot_datetime(args.slot_time)
    run_intraday_delta(
        slot_dt=slot_dt,
        symbols=args.symbols,
        use_test_symbols=args.test,
        trigger_type=args.trigger_type,
        run_post_hooks=not args.skip_post_hooks,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    main()
