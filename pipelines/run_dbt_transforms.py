"""Run dbt transformations after BQuant source data refreshes."""

from __future__ import annotations

import argparse
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from pipelines.live_update_runtime import create_run_id, load_live_update_config, record_pipeline_run
from utils.logger import BQuantLogger
from warehouse.duckdb_connection import get_connection
from warehouse.refresh_state import bump_refresh_version, ensure_refresh_state_table


REPO_ROOT = Path(__file__).resolve().parent.parent
PIPELINE_NAME = "run_dbt_transforms"
MART_REFRESH_DATASETS = {
    "analytics_mart_market_regime_daily": ("analytics_marts.mart_market_regime_daily", "trading_date"),
    "analytics_mart_symbol_daily_features": ("analytics_marts.mart_symbol_daily_features", "trading_date"),
    "analytics_mart_symbol_data_quality": ("analytics_marts.mart_symbol_data_quality", None),
    "analytics_mart_agent_context_daily": ("analytics_marts.mart_agent_context_daily", "trading_date"),
}


def parse_args() -> argparse.Namespace:
    """Parse CLI arguments for dbt transform execution.

    Returns:
        Namespace with trigger metadata, optional dbt selector, and test toggle.
    """
    parser = argparse.ArgumentParser(description="Run BQuant dbt transformations.")
    parser.add_argument("--trigger-type", default="manual", choices=["manual", "scheduled", "recovery"])
    parser.add_argument("--select", dest="selector", help="Optional dbt selector passed to run/test.")
    parser.add_argument("--skip-tests", action="store_true", help="Run dbt models without dbt tests.")
    return parser.parse_args()


def _dbt_config() -> dict[str, Any]:
    """Return the live-update dbt post-hook configuration block.

    Returns:
        `configs/live_update.yaml` block at `post_update.dbt`.
    """
    return load_live_update_config().get("post_update", {}).get("dbt", {})


def _command_prefix(config: dict[str, Any]) -> list[str]:
    """Resolve a dbt executable command with a conda fallback.

    Args:
        config: dbt post-update config block.

    Returns:
        Command prefix suitable for subprocess execution.
    """
    command = str(config.get("command", "dbt"))
    if shutil.which(command):
        return [command]
    if shutil.which("conda"):
        return ["conda", "run", "-n", "bquant", command]
    return [command]


def _build_dbt_command(config: dict[str, Any], subcommand: str, selector: str | None) -> list[str]:
    """Build the dbt CLI command for one subcommand.

    Args:
        config: dbt post-update config block.
        subcommand: dbt subcommand such as `run` or `test`.
        selector: Optional dbt selector.

    Returns:
        Fully expanded dbt command.
    """
    project_dir = str(config.get("project_dir", "transformations/dbt"))
    profiles_dir = str(config.get("profiles_dir", "transformations/dbt"))
    command = _command_prefix(config) + [
        subcommand,
        "--project-dir",
        project_dir,
        "--profiles-dir",
        profiles_dir,
    ]
    if selector:
        command.extend(["--select", selector])
    return command


def _run_command(command: list[str]) -> subprocess.CompletedProcess[str]:
    """Run one dbt command and capture output.

    Args:
        command: Fully expanded subprocess command.

    Returns:
        Completed process object.

    Raises:
        subprocess.CalledProcessError: When dbt exits non-zero.
    """
    completed = subprocess.run(
        command,
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise subprocess.CalledProcessError(
            completed.returncode,
            command,
            output=completed.stdout,
            stderr=completed.stderr,
        )
    return completed


def _tail_output(value: str, max_lines: int = 30) -> str:
    """Return the final lines of a potentially verbose dbt output string.

    Args:
        value: Raw stdout/stderr text.
        max_lines: Number of final lines to keep.

    Returns:
        Joined output tail.
    """
    lines = value.splitlines()
    return "\n".join(lines[-max_lines:])


def _latest_mart_timestamp(table_name: str, column_name: str | None) -> datetime | None:
    """Read the latest timestamp represented by one mart table.

    Args:
        table_name: Fully qualified DuckDB table name.
        column_name: Date/timestamp column to inspect. `None` means the mart has
            no temporal grain and should use current run time.

    Returns:
        Latest timestamp or `None` if the table is absent/empty.
    """
    if column_name is None:
        return datetime.now()
    try:
        with get_connection(read_only=True) as conn:
            value = conn.execute(f"SELECT max({column_name}) FROM {table_name}").fetchone()[0]
    except Exception:
        return None
    if value is None:
        return None
    return pd.Timestamp(value).to_pydatetime()


def _bump_mart_refresh_versions(run_id: str) -> dict[str, int]:
    """Bump refresh versions for dbt mart datasets.

    Args:
        run_id: Current dbt run id.

    Returns:
        Mapping from synthetic mart dataset name to refresh version.
    """
    ensure_refresh_state_table()
    versions: dict[str, int] = {}
    for dataset_name, (table_name, column_name) in MART_REFRESH_DATASETS.items():
        latest_ts = _latest_mart_timestamp(table_name, column_name)
        state = bump_refresh_version(dataset_name, run_id=run_id, latest_data_ts=latest_ts)
        versions[dataset_name] = int(state["refresh_version"])
    return versions


def run_dbt_transforms(
    *,
    trigger_type: str = "manual",
    selector: str | None = None,
    run_tests: bool | None = None,
) -> dict[str, Any]:
    """Run dbt models, optional tests, and refresh mart version state.

    Args:
        trigger_type: `manual`, `scheduled`, or `recovery`.
        selector: Optional dbt selector applied to `run` and `test`.
        run_tests: Override config-driven test execution.

    Returns:
        Summary dictionary with run id, status, and command metadata.

    Raises:
        subprocess.CalledProcessError: When dbt fails.
    """
    config = _dbt_config()
    run_id = create_run_id()
    logger = BQuantLogger(
        PIPELINE_NAME,
        component="pipeline",
        subcomponent=PIPELINE_NAME,
        default_channel="pipeline",
    ).with_run_context(run_id=run_id, trigger_type=trigger_type)
    start_time = datetime.now()
    started = time.perf_counter()
    steps_completed: list[str] = []
    steps_failed: list[str] = []

    tests_enabled = bool(config.get("test_on_eod_reconcile", True)) if run_tests is None else bool(run_tests)

    try:
        run_command = _build_dbt_command(config, "run", selector)
        logger.info(
            "Starting dbt run",
            event_type="job_start",
            status="running",
            command=run_command,
            selector=selector,
            tests_enabled=tests_enabled,
        )
        run_result = _run_command(run_command)
        steps_completed.append("dbt_run")

        test_result: subprocess.CompletedProcess[str] | None = None
        if tests_enabled:
            test_command = _build_dbt_command(config, "test", selector)
            test_result = _run_command(test_command)
            steps_completed.append("dbt_test")

        mart_versions = _bump_mart_refresh_versions(run_id)
        steps_completed.append("bump_mart_refresh_versions")

        finished_at = datetime.now()
        duration_seconds = time.perf_counter() - started
        record_pipeline_run(
            run_id=run_id,
            pipeline_name=PIPELINE_NAME,
            start_time=start_time,
            end_time=finished_at,
            status="success",
            input_rows=0,
            output_rows=len(mart_versions),
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
            duration_seconds=round(duration_seconds, 3),
            selector=selector,
            dbt_run_tail=_tail_output(run_result.stdout),
            dbt_test_tail=_tail_output(test_result.stdout) if test_result else None,
            mart_versions=mart_versions,
        )
        return {
            "run_id": run_id,
            "status": "success",
            "selector": selector,
            "run_tests": tests_enabled,
            "mart_versions": mart_versions,
        }
    except Exception as exc:
        finished_at = datetime.now()
        steps_failed.append("run_dbt_transforms")
        record_pipeline_run(
            run_id=run_id,
            pipeline_name=PIPELINE_NAME,
            start_time=start_time,
            end_time=finished_at,
            status="failed",
            input_rows=0,
            output_rows=0,
            error_message=str(exc),
        )
        context: dict[str, Any] = {
            "run_id": run_id,
            "trigger_type": trigger_type,
            "selector": selector,
            "steps_completed": steps_completed,
            "steps_failed": steps_failed,
        }
        if isinstance(exc, subprocess.CalledProcessError):
            context["stdout_tail"] = _tail_output(exc.output or "")
            context["stderr_tail"] = _tail_output(exc.stderr or "")
            context["command"] = exc.cmd
        logger.log_error(
            PIPELINE_NAME,
            type(exc).__name__,
            str(exc),
            context=context,
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
            selector=selector,
            duration_seconds=round(time.perf_counter() - started, 3),
        )
        raise


def main() -> None:
    """CLI entrypoint for dbt post-update transforms."""
    args = parse_args()
    run_dbt_transforms(
        trigger_type=args.trigger_type,
        selector=args.selector,
        run_tests=not args.skip_tests,
    )


if __name__ == "__main__":
    main()
