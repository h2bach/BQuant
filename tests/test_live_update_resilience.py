from __future__ import annotations

import unittest
from argparse import Namespace
from datetime import date, datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pandas as pd

from pipelines import evaluate_alerts as eval_alerts_module
from pipelines import live_update_worker as worker_module
from pipelines import run_eod_reconcile as eod_module
from pipelines import run_intraday_delta as intraday_module
from utils.logger import BQuantLogger


TZ = ZoneInfo("Asia/Ho_Chi_Minh")


class DummyLogger:
    def __init__(self, *args, **kwargs) -> None:
        pass

    def with_run_context(self, **kwargs):
        return self

    def info(self, *args, **kwargs) -> None:
        pass

    def warning(self, *args, **kwargs) -> None:
        pass

    def emit_event(self, *args, **kwargs) -> None:
        pass

    def log_pipeline_run(self, *args, **kwargs) -> None:
        pass

    def log_data_event(self, *args, **kwargs) -> None:
        pass

    def log_scheduler_event(self, *args, **kwargs) -> None:
        pass

    def log_alert_event(self, *args, **kwargs) -> None:
        pass

    def log_error(self, *args, **kwargs) -> None:
        pass


class FakeCursor:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows


class FakeObservabilityConnection:
    def __init__(self, pipeline_statuses: dict[str, list[tuple[str]]]) -> None:
        self.pipeline_statuses = pipeline_statuses

    def execute(self, sql: str, params=None):
        if "SELECT DISTINCT pipeline_name FROM obs_job_runs" in sql:
            return FakeCursor([(name,) for name in self.pipeline_statuses])
        if "SELECT status" in sql and "FROM obs_job_runs" in sql:
            pipeline_name = params[0]
            return FakeCursor(self.pipeline_statuses[pipeline_name])
        raise AssertionError(f"Unexpected SQL in fake observability connection: {sql}")


class FakeConnectionManager:
    def __init__(self, conn) -> None:
        self.conn = conn

    def __enter__(self):
        return self.conn

    def __exit__(self, exc_type, exc, tb):
        return False


class LiveUpdateResilienceTests(unittest.TestCase):
    def test_pipeline_logger_uses_non_error_levels_for_skipped_and_no_data(self) -> None:
        with patch.object(BQuantLogger, "emit_event", return_value={}) as emit_event:
            logger = BQuantLogger("test_pipeline_logger")
            logger.log_pipeline_run(
                pipeline_name="demo_pipeline",
                start_time="2026-06-24T10:00:00",
                end_time="2026-06-24T10:01:00",
                status="skipped",
                steps_completed=[],
                steps_failed=[],
            )
            self.assertEqual(emit_event.call_args.kwargs["level"], 20)
            self.assertEqual(emit_event.call_args.kwargs["legacy_category"], "pipeline")

            logger.log_pipeline_run(
                pipeline_name="demo_pipeline",
                start_time="2026-06-24T10:00:00",
                end_time="2026-06-24T10:01:00",
                status="no_data",
                steps_completed=[],
                steps_failed=[],
            )
            self.assertEqual(emit_event.call_args.kwargs["level"], 30)
            self.assertEqual(emit_event.call_args.kwargs["legacy_category"], "pipeline")

    @patch("pipelines.run_intraday_delta.BQuantLogger", new=DummyLogger)
    @patch("pipelines.run_intraday_delta.record_pipeline_run")
    @patch("pipelines.run_intraday_delta.bump_refresh_version")
    @patch("pipelines.run_intraday_delta.materialize_dataset_from_table")
    @patch("pipelines.run_intraday_delta.upsert_intraday_rows")
    @patch("pipelines.run_intraday_delta._fetch_symbol_delta", return_value=("VNM", pd.DataFrame(), None))
    @patch("pipelines.run_intraday_delta.source_config", return_value={"rate_limit": {"requests_per_minute": 120}})
    @patch("pipelines.run_intraday_delta.resolve_universe_symbols", return_value=["VNM"])
    @patch("pipelines.run_intraday_delta.load_live_update_config", return_value={"jobs": {"intraday_delta": {"max_parallel_symbols": 1}}})
    @patch("pipelines.run_intraday_delta.ensure_refresh_state_table")
    @patch("pipelines.run_intraday_delta.initialize_observability")
    def test_intraday_no_data_returns_no_data_without_writes(
        self,
        _init_obs,
        _ensure_refresh,
        _load_cfg,
        _resolve_symbols,
        _source_cfg,
        _fetch_symbol,
        upsert_rows,
        materialize_dataset,
        bump_refresh,
        record_run,
    ) -> None:
        result = intraday_module.run_intraday_delta(
            slot_dt=datetime(2026, 6, 24, 10, 15, tzinfo=TZ),
            run_post_hooks=False,
        )

        self.assertEqual(result["status"], "no_data")
        upsert_rows.assert_not_called()
        materialize_dataset.assert_not_called()
        bump_refresh.assert_not_called()
        record_run.assert_called_once()
        self.assertEqual(record_run.call_args.kwargs["status"], "no_data")

    @patch("pipelines.run_intraday_delta.BQuantLogger", new=DummyLogger)
    @patch("pipelines.run_intraday_delta.record_pipeline_run")
    @patch("pipelines.run_intraday_delta._fetch_symbol_delta")
    @patch("pipelines.run_intraday_delta.resolve_universe_symbols", return_value=["VNM"])
    @patch("pipelines.run_intraday_delta.load_live_update_config", return_value={"jobs": {"intraday_delta": {"max_parallel_symbols": 1}}})
    @patch("pipelines.run_intraday_delta.ensure_refresh_state_table")
    @patch("pipelines.run_intraday_delta.initialize_observability")
    def test_intraday_dry_run_skips_fetch_and_writes(
        self,
        _init_obs,
        _ensure_refresh,
        _load_cfg,
        _resolve_symbols,
        fetch_symbol,
        record_run,
    ) -> None:
        result = intraday_module.run_intraday_delta(
            slot_dt=datetime(2026, 6, 24, 10, 15, tzinfo=TZ),
            run_post_hooks=False,
            dry_run=True,
        )

        self.assertEqual(result["status"], "skipped")
        self.assertTrue(result["dry_run"])
        fetch_symbol.assert_not_called()
        record_run.assert_called_once()
        self.assertEqual(record_run.call_args.kwargs["status"], "skipped")

    @patch("pipelines.run_eod_reconcile.BQuantLogger", new=DummyLogger)
    @patch("pipelines.run_eod_reconcile.record_pipeline_run")
    @patch("pipelines.run_eod_reconcile.bump_refresh_version")
    @patch("pipelines.run_eod_reconcile.clear_dataset_materialization")
    @patch("pipelines.run_eod_reconcile.materialize_dataset_from_table")
    @patch("pipelines.run_eod_reconcile._trim_intraday_base_window")
    @patch("pipelines.run_eod_reconcile._clear_delta_rows")
    @patch("pipelines.run_eod_reconcile.upsert_intraday_rows")
    @patch("pipelines.run_eod_reconcile.upsert_daily_rows")
    @patch("pipelines.run_eod_reconcile._load_delta_rows", return_value=pd.DataFrame())
    @patch("pipelines.run_eod_reconcile._fetch_symbol_daily_latest", return_value=pd.DataFrame())
    @patch("pipelines.run_eod_reconcile.source_config", return_value={"rate_limit": {"requests_per_minute": 120}})
    @patch("pipelines.run_eod_reconcile.resolve_universe_symbols", return_value=["VNM"])
    @patch("pipelines.run_eod_reconcile.load_live_update_config", return_value={"execution": {"parallel_workers": 1}})
    @patch("pipelines.run_eod_reconcile.ensure_refresh_state_table")
    @patch("pipelines.run_eod_reconcile.initialize_observability")
    def test_eod_no_data_returns_no_data_without_writes(
        self,
        _init_obs,
        _ensure_refresh,
        _load_cfg,
        _resolve_symbols,
        _source_cfg,
        _fetch_daily,
        _load_delta,
        upsert_daily,
        upsert_intraday,
        clear_delta,
        trim_base,
        materialize_dataset,
        clear_materialization,
        bump_refresh,
        record_run,
    ) -> None:
        result = eod_module.run_eod_reconcile(
            trade_date=date(2026, 6, 24),
            run_post_hooks=False,
        )

        self.assertEqual(result["status"], "no_data")
        upsert_daily.assert_not_called()
        upsert_intraday.assert_not_called()
        clear_delta.assert_not_called()
        trim_base.assert_not_called()
        materialize_dataset.assert_not_called()
        clear_materialization.assert_not_called()
        bump_refresh.assert_not_called()
        record_run.assert_called_once()
        self.assertEqual(record_run.call_args.kwargs["status"], "no_data")

    @patch("pipelines.run_eod_reconcile.BQuantLogger", new=DummyLogger)
    @patch("pipelines.run_eod_reconcile.record_pipeline_run")
    @patch("pipelines.run_eod_reconcile._fetch_symbol_daily_latest")
    @patch("pipelines.run_eod_reconcile.resolve_universe_symbols", return_value=["VNM"])
    @patch("pipelines.run_eod_reconcile.load_live_update_config", return_value={"execution": {"parallel_workers": 1}})
    @patch("pipelines.run_eod_reconcile.ensure_refresh_state_table")
    @patch("pipelines.run_eod_reconcile.initialize_observability")
    def test_eod_dry_run_skips_fetch_and_writes(
        self,
        _init_obs,
        _ensure_refresh,
        _load_cfg,
        _resolve_symbols,
        fetch_daily,
        record_run,
    ) -> None:
        result = eod_module.run_eod_reconcile(
            trade_date=date(2026, 6, 24),
            run_post_hooks=False,
            dry_run=True,
        )

        self.assertEqual(result["status"], "skipped")
        self.assertTrue(result["dry_run"])
        fetch_daily.assert_not_called()
        record_run.assert_called_once()
        self.assertEqual(record_run.call_args.kwargs["status"], "skipped")

    @patch("pipelines.live_update_worker.BQuantLogger", new=DummyLogger)
    @patch("pipelines.live_update_worker.record_pipeline_run")
    @patch("pipelines.live_update_worker.run_eod_reconcile")
    @patch("pipelines.live_update_worker.run_intraday_delta")
    @patch("pipelines.live_update_worker.evaluate_alerts")
    @patch("pipelines.live_update_worker.ingest_observability_logs")
    @patch("pipelines.live_update_worker.get_plot_floor_timestamp", return_value=None)
    @patch("pipelines.live_update_worker.recent_due_intraday_slots")
    @patch("pipelines.live_update_worker.market_session_state", return_value="intraday_morning")
    @patch("pipelines.live_update_worker.is_trading_day", return_value=True)
    @patch("pipelines.live_update_worker.now_local")
    @patch("pipelines.live_update_worker.load_live_update_config")
    @patch("pipelines.live_update_worker.initialize_observability")
    @patch("pipelines.live_update_worker.parse_args", return_value=Namespace(once=True, dry_run=True))
    def test_worker_once_dry_run_does_not_dispatch_jobs(
        self,
        _parse_args,
        _init_obs,
        load_cfg,
        now_local,
        _is_trading_day,
        _session_state,
        due_slots,
        _plot_floor,
        ingest_logs,
        evaluate_alerts,
        run_intraday,
        run_eod,
        record_run,
    ) -> None:
        current = datetime(2026, 6, 24, 10, 20, tzinfo=TZ)
        now_local.return_value = current
        load_cfg.return_value = {
            "worker": {
                "name": "test_worker",
                "poll_interval_seconds": 60,
                "ingest_logs_every_seconds": 60,
                "evaluate_alerts_every_seconds": 60,
                "consecutive_failure_escalation": 2,
            }
        }
        due_slots.return_value = [current - timedelta(minutes=5)]

        with patch("pipelines.live_update_worker.eod_reconcile_time", return_value=current + timedelta(hours=1)):
            worker_module.main()

        run_intraday.assert_not_called()
        run_eod.assert_not_called()
        self.assertGreaterEqual(ingest_logs.call_count, 1)
        evaluate_alerts.assert_called_once()
        record_run.assert_called_once()
        self.assertEqual(record_run.call_args.kwargs["status"], "success")

    def test_consecutive_failures_ignores_no_data_and_skipped_statuses(self) -> None:
        fake_conn = FakeConnectionManager(
            FakeObservabilityConnection(
                {
                    "run_intraday_delta": [("no_data",), ("failed",)],
                    "run_eod_reconcile": [("skipped",), ("failed",)],
                }
            )
        )
        with (
            patch("pipelines.evaluate_alerts.get_observability_connection", return_value=fake_conn),
            patch("pipelines.evaluate_alerts._load_thresholds", return_value={"consecutive_job_failures": 2}),
            patch("pipelines.evaluate_alerts._emit_alert_state_change") as emit_alert,
        ):
            eval_alerts_module._evaluate_consecutive_failures(DummyLogger(), {})

        emit_alert.assert_not_called()

    def test_consecutive_failures_opens_only_for_failed_runs(self) -> None:
        fake_conn = FakeConnectionManager(
            FakeObservabilityConnection({"run_intraday_delta": [("failed",), ("failed",)]})
        )
        with (
            patch("pipelines.evaluate_alerts.get_observability_connection", return_value=fake_conn),
            patch("pipelines.evaluate_alerts._load_thresholds", return_value={"consecutive_job_failures": 2}),
            patch("pipelines.evaluate_alerts._emit_alert_state_change") as emit_alert,
        ):
            eval_alerts_module._evaluate_consecutive_failures(DummyLogger(), {})

        emit_alert.assert_called_once()


if __name__ == "__main__":
    unittest.main()
