"""Data catalog page for BQuant Platform."""

from __future__ import annotations

from pathlib import Path

from nicegui import ui
from nicegui.client import Client

from utils.logger import BQuantLogger
from warehouse.duckdb_connection import get_connection

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
LOGGER = BQuantLogger("web_catalog", component="web", subcomponent="catalog", default_channel="web")


def render_catalog(client: Client):
    """Render the data catalog page."""
    del client
    ui.label("Data Catalog").classes("bq-page-title font-bold")
    ui.separator()
    
    with ui.row().classes("bq-toolbar w-full"):
        ui.button("Back to Dashboard", on_click=lambda: ui.navigate.to("/"))
    
    ui.separator()
    
    datasets = get_dataset_catalog()
    
    with ui.row().classes("bq-card-grid w-full"):
        for dataset in datasets:
            with ui.card().classes("bq-card"):
                ui.label(dataset["name"]).classes("text-xl font-semibold")
                ui.label(f"Description: {dataset['description']}")
                ui.label(f"Table: {dataset['table']}")
                ui.label(f"Rows: {dataset['row_count']:,}")
                ui.label(f"Last Updated: {dataset['last_updated']}")
                ui.label(f"Storage: {dataset['storage']}")


def get_dataset_catalog() -> list[dict[str, str | int]]:
    """Get dataset catalog information from DuckDB."""
    datasets = []
    try:
        with get_connection(read_only=True) as conn:
            # Get table information
            tables = conn.execute("""
                SELECT table_name, 
                       (SELECT COUNT(*) FROM information_schema.columns WHERE table_name = t.table_name) as column_count
                FROM information_schema.tables t
                WHERE table_schema = 'main' AND table_type = 'BASE TABLE'
                ORDER BY table_name
            """).fetchall()
            
            for table_name, column_count in tables:
                row_count = conn.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()[0]
                
                # Map table names to descriptions
                descriptions = {
                    "universe_members": "VN30 universe membership with effective dates",
                    "daily_ohlcv_base": "10-year daily OHLCV from vnstock:VCI",
                    "market_index_daily_base": "10-year VNINDEX/VN30 daily OHLCV from vnstock:VCI",
                    "intraday_ohlcv_15m_base": "60-day 15-minute intraday OHLCV snapshot",
                    "intraday_ohlcv_15m_delta": "Intraday delta bars after base snapshot",
                    "clean_ohlcv_daily": "Cleaned and validated daily OHLCV",
                    "clean_ohlcv_hourly": "Cleaned and validated hourly OHLCV",
                    "technical_features_daily": "Technical indicators and features",
                    "combined_features_daily": "Combined features with scores and targets",
                    "trading_signals": "Daily trading signals",
                    "backtest_runs": "Backtest execution results",
                    "pipeline_runs": "Pipeline execution logs",
                    "data_file_manifest": "Dataset file manifest and metadata",
                    "metadata": "System metadata and version info",
                }
                
                datasets.append({
                    "name": table_name,
                    "description": descriptions.get(table_name, "No description"),
                    "table": table_name,
                    "row_count": row_count,
                    "column_count": column_count,
                    "last_updated": "N/A",
                    "storage": "DuckDB + Parquet",
                })
    except Exception as e:
        LOGGER.log_error("get_dataset_catalog", type(e).__name__, str(e), context={}, channel="web")
    return datasets
