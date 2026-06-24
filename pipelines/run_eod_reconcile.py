"""Run end-of-day reconciliation for daily and intraday base datasets."""

from __future__ import annotations

import argparse
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, time as dtime, timedelta
from typing import Any

import pandas as pd

from data_ingestion.fetch_daily_10y_base import upsert_daily_rows
from data_ingestion.fetch_intraday_15m import upsert_intraday_rows
from data_ingestion.yfinance_adapter import fetch_daily_history, source_config
from utils.logger import BQuantLogger
from utils.rate_limit import SlidingWindowRateLimiter
from warehouse.data_manifest import clear_dataset_materialization, materialize_dataset_from_table
from warehouse.duckdb_connection import get_connection
from warehouse.init_observability import initialize_observability
from warehouse.refresh_state import bump_refresh_version, ensure_refresh_state_table

from pipelines.evaluate_alerts import evaluate_alerts
from pipelines.ingest_observability_logs import ingest_observability_logs
from pipelines.live_update_runtime import (
    base_intraday_lookback_days,
    create_run_id,
    get_latest_daily_date,
    get_latest_plot_timestamp,
    is_trading_day,
    load_live_update_config,
    market_timezone,
    now_local,
    record_pipeline_run,
    resolve_universe_symbols,
)


PIPELINE_NAME = "run_eod_reconcile"


def parse_args() -> argparse.Namespace:
    """Parse CLI arguments for the end-of-day reconcile job."""
    parser = argparse.ArgumentParser(description="Run end-of-day reconcile for live update datasets.")
    parser.add_argument("--trade-date", help="Trade date in YYYY-MM-DD format.")
    parser.add_argument("--symbols", nargs="*", help="Optional symbol subset.")
    parser.add_argument("--test", action="store_true", help="Use test universe only.")
    parser.add_argument("--trigger-type", default="manual", choices=["manual", "scheduled", "recovery"])
    parser.add_argument("--skip-post-hooks", action="store_true", help="Skip observability ingest and alert evaluation.")
    parser.add_argument("--dry-run", action="store_true", help="Resolve trade date and symbols, but do not fetch or write data.")
    return parser.parse_args()


def _trade_date(args_trade_date: str | None) -> datetime.date:
    """Resolve the target trade date from CLI input or the current local date."""
    if args_trade_date:
        return datetime.fromisoformat(args_trade_date).date()
    return now_local().date()


def _fetch_symbol_daily_latest(
    symbol: str,
    trade_date: datetime.date,
    limiter: SlidingWindowRateLimiter,
    logger: BQuantLogger,
) -> pd.DataFrame:
    """Fetch the latest daily bar for one symbol as part of EOD reconciliation."""
    request_id = create_run_id()
    request_start = datetime.now()
    waited = limiter.acquire()
    try:
        if waited > 0:
            logger.emit_event(
                "Paused for source rate limit before daily refresh",
                level=logging.INFO,
                channel="ingestion",
                event_type="rate_limit_wait",
                legacy_category="data",
                data_subcategory="ingestion",
                symbol=symbol,
                dataset_name="daily_ohlcv_10y",
                wait_seconds=round(waited, 2),
            )
        frame = fetch_daily_history(symbol, trade_date.isoformat(), trade_date.isoformat())
        request_end = datetime.now()
        logger.emit_event(
            f"Completed daily latest-bar source request for {symbol}",
            level=logging.INFO,
            channel="ingestion",
            event_type="source_request",
            status="empty" if frame.empty else "success",
            legacy_category="data",
            data_subcategory="ingestion",
            dataset_name="daily_ohlcv_10y",
            symbol=symbol,
            request_id=request_id,
            provider="yfinance",
            request_start=request_start.isoformat(),
            request_end=request_end.isoformat(),
            duration_seconds=round((request_end - request_start).total_seconds(), 3),
            rows_fetched=int(len(frame)),
            wait_seconds=round(waited, 3),
            empty_payload=bool(frame.empty),
            trade_date=trade_date.isoformat(),
        )
        return frame
    except Exception as exc:
        request_end = datetime.now()
        logger.emit_event(
            f"Daily source request failed for {symbol}",
            level=logging.ERROR,
            channel="ingestion",
            event_type="source_request",
            status="failed",
            legacy_category="error",
            dataset_name="daily_ohlcv_10y",
            symbol=symbol,
            request_id=request_id,
            provider="yfinance",
            request_start=request_start.isoformat(),
            request_end=request_end.isoformat(),
            duration_seconds=round((request_end - request_start).total_seconds(), 3),
            rows_fetched=0,
            wait_seconds=round(waited, 3),
            empty_payload=False,
            trade_date=trade_date.isoformat(),
            error_type=type(exc).__name__,
            error_message=str(exc),
        )
        return pd.DataFrame()


def _load_delta_rows(trade_date: datetime.date) -> pd.DataFrame:
    """Load delta intraday rows that should be merged into the intraday base dataset."""
    with get_connection(read_only=True) as conn:
        frame = conn.execute(
            """
            SELECT symbol, bar_time, session_date, open, high, low, close, volume, interval, source, snapshot_date
            FROM intraday_ohlcv_15m_delta
            WHERE session_date <= ?
            ORDER BY symbol, bar_time
            """,
            [trade_date],
        ).df()
    if frame.empty:
        return frame
    frame["bar_time"] = pd.to_datetime(frame["bar_time"])
    return frame


def _clear_delta_rows(trade_date: datetime.date) -> int:
    """Delete reconciled delta rows up to the supplied trade date."""
    with get_connection(read_only=False) as conn:
        count = conn.execute(
            "SELECT count(*) FROM intraday_ohlcv_15m_delta WHERE session_date <= ?",
            [trade_date],
        ).fetchone()[0]
        conn.execute("DELETE FROM intraday_ohlcv_15m_delta WHERE session_date <= ?", [trade_date])
    return int(count)


def _trim_intraday_base_window(trade_date: datetime.date) -> int:
    """Trim intraday base rows older than the configured rolling lookback window."""
    cutoff = trade_date - timedelta(days=base_intraday_lookback_days())
    with get_connection(read_only=False) as conn:
        count = conn.execute(
            "SELECT count(*) FROM intraday_ohlcv_15m_base WHERE session_date < ?",
            [cutoff],
        ).fetchone()[0]
        conn.execute("DELETE FROM intraday_ohlcv_15m_base WHERE session_date < ?", [cutoff])
    return int(count)


def run_eod_reconcile(
    *,
    trade_date: datetime.date | None = None,
    symbols: list[str] | None = None,
    use_test_symbols: bool = False,
    trigger_type: str = "manual",
    run_post_hooks: bool = True,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Run the EOD daily refresh plus intraday base merge and delta cleanup."""
    initialize_observability()
    ensure_refresh_state_table()

    run_id = create_run_id()
    trade_date = trade_date or now_local().date()
    cfg = load_live_update_config()
    job_logger = BQuantLogger(
        PIPELINE_NAME,
        component="pipeline",
        subcomponent=PIPELINE_NAME,
        default_channel="pipeline",
    ).with_run_context(run_id=run_id, trigger_type=trigger_type)
    start_time = datetime.now()
    perf_started = time.perf_counter()
    steps_completed: list[str] = []
    steps_failed: list[str] = []
    total_fetched_rows = 0
    total_saved_rows = 0

    if not is_trading_day(trade_date) and trigger_type != "manual":
        record_pipeline_run(
            run_id=run_id,
            pipeline_name=PIPELINE_NAME,
            start_time=start_time,
            end_time=datetime.now(),
            status="skipped",
            input_rows=0,
            output_rows=0,
            error_message="Trade date is not a configured trading day.",
        )
        job_logger.log_pipeline_run(
            pipeline_name=PIPELINE_NAME,
            start_time=start_time.isoformat(),
            end_time=datetime.now().isoformat(),
            status="skipped",
            steps_completed=[],
            steps_failed=["trading_day_check"],
            error_message="Trade date is not a configured trading day.",
            run_id=run_id,
            trigger_type=trigger_type,
            trade_date=trade_date.isoformat(),
        )
        return {"run_id": run_id, "status": "skipped", "trade_date": trade_date.isoformat()}

    target_symbols = resolve_universe_symbols(explicit_symbols=symbols, use_test=use_test_symbols)
    rpm = int(source_config().get("rate_limit", {}).get("requests_per_minute", 120))
    max_workers = max(int(cfg.get("execution", {}).get("parallel_workers", 4)), 1)
    limiter = SlidingWindowRateLimiter(rpm)

    try:
        job_logger.info(
            "Starting EOD reconcile",
            event_type="job_start",
            status="running",
            trade_date=trade_date.isoformat(),
            symbols=target_symbols,
            max_workers=max_workers,
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
                steps_completed=["resolve_trade_date", "resolve_symbols"],
                steps_failed=[],
                run_id=run_id,
                trigger_type=trigger_type,
                trade_date=trade_date.isoformat(),
                dry_run=True,
            )
            return {
                "run_id": run_id,
                "status": "skipped",
                "trade_date": trade_date.isoformat(),
                "rows_fetched": 0,
                "rows_saved": 0,
                "dry_run": True,
            }

        frames: list[pd.DataFrame] = []
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(_fetch_symbol_daily_latest, symbol, trade_date, limiter, job_logger): symbol
                for symbol in target_symbols
            }
            for future in as_completed(futures):
                frame = future.result()
                total_fetched_rows += int(len(frame))
                if not frame.empty:
                    frames.append(frame)
        steps_completed.append("fetch_daily_latest")

        combined_daily = (
            pd.concat(frames, ignore_index=True).sort_values(["symbol", "trading_date"]).reset_index(drop=True)
            if frames
            else pd.DataFrame()
        )
        delta_df = _load_delta_rows(trade_date)
        if combined_daily.empty and delta_df.empty:
            finished_at = datetime.now()
            record_pipeline_run(
                run_id=run_id,
                pipeline_name=PIPELINE_NAME,
                start_time=start_time,
                end_time=finished_at,
                status="no_data",
                input_rows=0,
                output_rows=0,
                error_message="No daily latest bar or intraday delta rows available for EOD reconcile.",
            )
            job_logger.emit_event(
                "No new daily or intraday rows available for EOD reconcile",
                level=logging.WARNING,
                channel="pipeline",
                event_type="no_data",
                status="no_data",
                trade_date=trade_date.isoformat(),
                symbols=target_symbols,
            )
            job_logger.log_pipeline_run(
                pipeline_name=PIPELINE_NAME,
                start_time=start_time.isoformat(),
                end_time=finished_at.isoformat(),
                status="no_data",
                steps_completed=["fetch_daily_latest"],
                steps_failed=[],
                error_message="No daily latest bar or intraday delta rows available for EOD reconcile.",
                run_id=run_id,
                trigger_type=trigger_type,
                input_rows=0,
                output_rows=0,
                duration_seconds=round(time.perf_counter() - perf_started, 3),
                trade_date=trade_date.isoformat(),
            )
            if run_post_hooks:
                ingest_observability_logs()
                evaluate_alerts(trigger_type=trigger_type)
                ingest_observability_logs(channel="alerts")
            return {
                "run_id": run_id,
                "status": "no_data",
                "trade_date": trade_date.isoformat(),
                "rows_fetched": 0,
                "rows_saved": 0,
            }

        total_saved_rows += upsert_daily_rows(combined_daily)
        steps_completed.append("upsert_daily")

        daily_materialized = materialize_dataset_from_table("daily_ohlcv_10y", symbols=target_symbols)
        steps_completed.append("materialize_daily")

        # The daily refresh and delta merge are kept as separate steps so failures
        # remain diagnosable and reruns stay idempotent at the table-slice level.
        merged_intraday_rows = 0
        if not delta_df.empty:
            merged_intraday_rows = upsert_intraday_rows("intraday_ohlcv_15m_base", delta_df)
        total_saved_rows += merged_intraday_rows
        steps_completed.append("merge_delta_to_base")

        cleared_rows = _clear_delta_rows(trade_date)
        trimmed_rows = _trim_intraday_base_window(trade_date)
        steps_completed.append("clear_delta_and_trim_base")

        base_materialized = materialize_dataset_from_table(
            "intraday_ohlcv_15m_60d",
            symbols=target_symbols,
            snapshot_date=trade_date,
        )
        delta_materialized = clear_dataset_materialization(
            "intraday_ohlcv_15m_delta",
            symbols=target_symbols,
            snapshot_date=trade_date,
        )
        steps_completed.append("materialize_intraday_files")

        daily_latest = get_latest_daily_date()
        daily_refresh = bump_refresh_version(
            "daily_ohlcv_10y",
            run_id=run_id,
            latest_data_ts=datetime.combine(daily_latest, dtime.min) if daily_latest else None,
            last_success_at=datetime.now(),
        )
        base_latest = get_latest_plot_timestamp()
        intraday_base_refresh = bump_refresh_version(
            "intraday_ohlcv_15m_60d",
            run_id=run_id,
            latest_data_ts=base_latest,
            last_success_at=datetime.now(),
        )
        intraday_delta_refresh = bump_refresh_version(
            "intraday_ohlcv_15m_delta",
            run_id=run_id,
            latest_data_ts=None,
            last_success_at=datetime.now(),
        )
        steps_completed.append("refresh_versions")

        # Emit checkpoint events for both the refreshed base and the cleared delta
        # so the observability projection can show reconciliation state per symbol.
        for symbol in target_symbols:
            latest_base_ts = get_latest_plot_timestamp(symbol)
            job_logger.emit_event(
                f"Updated base intraday checkpoint for {symbol}",
                level=logging.INFO,
                channel="pipeline",
                event_type="checkpoint_updated",
                status="success",
                dataset_name="intraday_ohlcv_15m_60d",
                symbol=symbol,
                watermark_ts=latest_base_ts.isoformat() if latest_base_ts else None,
                trade_date=trade_date.isoformat(),
            )
            job_logger.emit_event(
                f"Cleared delta checkpoint for {symbol}",
                level=logging.INFO,
                channel="pipeline",
                event_type="checkpoint_updated",
                status="cleared",
                dataset_name="intraday_ohlcv_15m_delta",
                symbol=symbol,
                watermark_ts=None,
                trade_date=trade_date.isoformat(),
            )
        steps_completed.append("emit_checkpoints")

        finished_at = datetime.now()
        record_pipeline_run(
            run_id=run_id,
            pipeline_name=PIPELINE_NAME,
            start_time=start_time,
            end_time=finished_at,
            status="success",
            input_rows=total_fetched_rows + int(len(delta_df)),
            output_rows=total_saved_rows,
        )
        steps_completed.append("record_pipeline_run")

        job_logger.emit_event(
            "Bumped refresh versions after EOD reconcile",
            level=logging.INFO,
            channel="pipeline",
            event_type="dataset_refresh",
            status="success",
            refresh_versions={
                "daily_ohlcv_10y": daily_refresh["refresh_version"],
                "intraday_ohlcv_15m_60d": intraday_base_refresh["refresh_version"],
                "intraday_ohlcv_15m_delta": intraday_delta_refresh["refresh_version"],
            },
            trade_date=trade_date.isoformat(),
        )
        job_logger.log_data_event(
            "ingestion",
            "EOD reconcile completed",
            event_type="dataset_refresh",
            status="success",
            trade_date=trade_date.isoformat(),
            rows_fetched=total_fetched_rows + int(len(delta_df)),
            rows_saved=total_saved_rows,
            merged_intraday_rows=merged_intraday_rows,
            cleared_delta_rows=cleared_rows,
            trimmed_base_rows=trimmed_rows,
            daily_materialized=daily_materialized,
            base_materialized=base_materialized,
            delta_materialized=delta_materialized,
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
            input_rows=total_fetched_rows + int(len(delta_df)),
            output_rows=total_saved_rows,
            duration_seconds=round(time.perf_counter() - perf_started, 3),
            trade_date=trade_date.isoformat(),
        )

        if run_post_hooks:
            ingest_observability_logs()
            evaluate_alerts(trigger_type=trigger_type)
            ingest_observability_logs(channel="alerts")

        return {
            "run_id": run_id,
            "status": "success",
            "trade_date": trade_date.isoformat(),
            "rows_fetched": total_fetched_rows + int(len(delta_df)),
            "rows_saved": total_saved_rows,
        }
    except Exception as exc:
        finished_at = datetime.now()
        steps_failed.append("run_eod_reconcile")
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
                "trade_date": trade_date.isoformat(),
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
            input_rows=total_fetched_rows,
            output_rows=total_saved_rows,
            duration_seconds=round(time.perf_counter() - perf_started, 3),
            trade_date=trade_date.isoformat(),
        )
        if run_post_hooks:
            ingest_observability_logs()
            evaluate_alerts(trigger_type=trigger_type)
            ingest_observability_logs(channel="alerts")
        raise


def main() -> None:
    """CLI entrypoint for the end-of-day reconcile job."""
    args = parse_args()
    run_eod_reconcile(
        trade_date=_trade_date(args.trade_date),
        symbols=args.symbols,
        use_test_symbols=args.test,
        trigger_type=args.trigger_type,
        run_post_hooks=not args.skip_post_hooks,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    main()
