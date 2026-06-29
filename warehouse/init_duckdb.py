"""Initialize the BQuant DuckDB database and schema."""

from __future__ import annotations

import time
import uuid
from datetime import datetime
from pathlib import Path

from utils.logger import BQuantLogger
from warehouse.data_manifest import initialize_manifest_file
from warehouse.duckdb_connection import execute_sql_file, get_connection, get_duckdb_path
from warehouse.init_observability import initialize_observability
from warehouse.refresh_state import ensure_refresh_state_table


REPO_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = REPO_ROOT / "warehouse" / "schema_duckdb.sql"
PIPELINE_NAME = "init_duckdb"


def ensure_required_directories() -> None:
    for relative in [
        "warehouse",
        "logs/pipeline",
        "logs/errors",
        "logs/data",
        "logs/observability/jsonl/scheduler",
        "logs/observability/jsonl/pipeline",
        "logs/observability/jsonl/ingestion",
        "logs/observability/jsonl/web",
        "logs/observability/jsonl/alerts",
        "logs/observability/dead_letter",
        "logs/observability/parquet",
        "data/base/daily_10y",
        "data/base/market_index_daily_10y",
        "data/base/intraday_15m_60d",
        "data/base/intraday_15m_delta",
        "data/metadata",
        "data/raw/ohlcv/source=vnquant",
        "data/raw/ohlcv_1h/source=vnstock_vci",
        "data/silver/clean_ohlcv_daily",
        "data/silver/clean_ohlcv_hourly",
        "data/silver/universe_members",
        "data/gold/technical_features_daily",
        "data/gold/combined_features_daily",
        "data/marts/trading_signals",
        "data/marts/backtest_results",
    ]:
        (REPO_ROOT / relative).mkdir(parents=True, exist_ok=True)


def verify_database_state() -> dict[str, object]:
    expected_tables = [
        "universe_members",
        "raw_ohlcv",
        "raw_ohlcv_hourly",
        "clean_ohlcv_daily",
        "clean_ohlcv_hourly",
        "daily_ohlcv_base",
        "market_index_daily_base",
        "intraday_ohlcv_15m_base",
        "intraday_ohlcv_15m_delta",
        "data_file_manifest",
        "technical_features_daily",
        "combined_features_daily",
        "trading_signals",
        "agent_recommendation_runs",
        "agent_recommendations",
        "agent_system_analysis_runs",
        "backtest_runs",
        "pipeline_runs",
        "dataset_refresh_state",
        "metadata",
    ]
    expected_views = [
        "v_latest_signals",
        "v_latest_agent_recommendations",
        "v_latest_agent_system_analysis",
        "v_universe_members",
        "v_clean_ohlcv_universe",
        "v_clean_ohlcv_hourly_universe",
        "v_daily_ohlcv_base_universe",
        "v_market_index_daily_base",
        "v_intraday_15m_base_universe",
        "v_intraday_15m_plot_universe",
        "v_data_file_manifest_current",
        "v_technical_features_universe",
        "v_combined_features_universe",
        "v_backtest_summary",
    ]

    with get_connection(read_only=True) as conn:
        actual_tables = {
            row[0]
            for row in conn.execute(
                """
                SELECT table_name
                FROM information_schema.tables
                WHERE table_schema = 'main' AND table_type = 'BASE TABLE'
                """
            ).fetchall()
        }
        actual_views = {
            row[0]
            for row in conn.execute(
                """
                SELECT table_name
                FROM information_schema.views
                WHERE table_schema = 'main'
                """
            ).fetchall()
        }

    missing_tables = sorted(set(expected_tables) - actual_tables)
    missing_views = sorted(set(expected_views) - actual_views)
    return {
        "expected_tables": expected_tables,
        "expected_views": expected_views,
        "missing_tables": missing_tables,
        "missing_views": missing_views,
        "table_count": len(actual_tables),
        "view_count": len(actual_views),
    }


def record_pipeline_run(
    *,
    status: str,
    start_time: datetime,
    end_time: datetime,
    error_message: str | None = None,
) -> None:
    with get_connection(read_only=False) as conn:
        conn.execute(
            """
            INSERT INTO pipeline_runs (
                run_id,
                pipeline_name,
                start_time,
                end_time,
                status,
                input_rows,
                output_rows,
                error_message
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                str(uuid.uuid4()),
                PIPELINE_NAME,
                start_time,
                end_time,
                status,
                0,
                0,
                error_message,
            ],
        )


def main() -> None:
    logger = BQuantLogger(PIPELINE_NAME)
    start_time = datetime.now()
    start_perf = time.perf_counter()
    steps_completed: list[str] = []
    steps_failed: list[str] = []

    try:
        ensure_required_directories()
        steps_completed.append("ensure_directories")

        logger.info(
            "Starting DuckDB initialization",
            pipeline_name=PIPELINE_NAME,
            database_path=get_duckdb_path(),
            schema_path=str(SCHEMA_PATH),
        )

        execute_sql_file(str(SCHEMA_PATH))
        steps_completed.append("execute_schema")

        initialize_manifest_file()
        steps_completed.append("initialize_manifest_file")

        ensure_refresh_state_table()
        steps_completed.append("ensure_refresh_state")

        initialize_observability()
        steps_completed.append("initialize_observability")

        verification = verify_database_state()
        if verification["missing_tables"] or verification["missing_views"]:
            raise RuntimeError(
                "Database verification failed: "
                f"missing_tables={verification['missing_tables']} "
                f"missing_views={verification['missing_views']}"
            )
        steps_completed.append("verify_database")

        end_time = datetime.now()
        duration_seconds = time.perf_counter() - start_perf
        record_pipeline_run(status="success", start_time=start_time, end_time=end_time)
        steps_completed.append("record_pipeline_run")

        logger.log_pipeline_run(
            pipeline_name=PIPELINE_NAME,
            start_time=start_time.isoformat(),
            end_time=end_time.isoformat(),
            status="success",
            steps_completed=steps_completed,
            steps_failed=steps_failed,
            duration_seconds=duration_seconds,
            database_path=get_duckdb_path(),
            **verification,
        )
    except Exception as exc:
        end_time = datetime.now()
        duration_seconds = time.perf_counter() - start_perf
        failed_step = "database_initialization"
        steps_failed.append(failed_step)
        logger.log_error(
            PIPELINE_NAME,
            type(exc).__name__,
            str(exc),
            context={
                "steps_completed": steps_completed,
                "steps_failed": steps_failed,
                "duration_seconds": duration_seconds,
                "database_path": get_duckdb_path(),
            },
        )
        try:
            if Path(get_duckdb_path()).exists():
                record_pipeline_run(
                    status="failed",
                    start_time=start_time,
                    end_time=end_time,
                    error_message=str(exc),
                )
        except Exception as record_exc:
            logger.log_error(
                "record_pipeline_run",
                type(record_exc).__name__,
                str(record_exc),
                context={"original_error": str(exc)},
            )

        logger.log_pipeline_run(
            pipeline_name=PIPELINE_NAME,
            start_time=start_time.isoformat(),
            end_time=end_time.isoformat(),
            status="failed",
            steps_completed=steps_completed,
            steps_failed=steps_failed,
            error_message=str(exc),
            duration_seconds=duration_seconds,
            database_path=get_duckdb_path(),
        )
        raise


if __name__ == "__main__":
    main()
