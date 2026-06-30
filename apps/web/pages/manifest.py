"""Data manifest page for BQuant Platform."""

from __future__ import annotations

from pathlib import Path

from nicegui import ui
from nicegui.client import Client

from utils.logger import BQuantLogger
from warehouse.duckdb_connection import get_connection

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
LOGGER = BQuantLogger("web_manifest", component="web", subcomponent="manifest", default_channel="web")


def render_manifest(client: Client):
    """Render the data manifest page."""
    del client
    ui.label("Data Manifest").classes("bq-page-title font-bold")
    ui.separator()
    
    with ui.row().classes("bq-toolbar w-full"):
        ui.button("Back to Dashboard", on_click=lambda: ui.navigate.to("/"))
    
    ui.separator()
    
    # Filter options
    with ui.row().classes("bq-control-row w-full"):
        dataset_filter = ui.select(
            options=["all", "daily_ohlcv_10y", "intraday_ohlcv_15m_60d", "intraday_ohlcv_15m_delta"],
            value="all"
        ).classes("bq-control-field-wide")
        status_filter = ui.select(
            options=["all", "up_to_date", "stale", "awaiting_refresh_window", "pending_merge"],
            value="all"
        ).classes("bq-control-field-wide")
    
    ui.separator()
    
    # Manifest table
    with ui.card().classes("bq-table-card w-full"):
        ui.label("Dataset Files").classes("text-xl font-semibold")
        columns = [
            {"name": "dataset_name", "label": "Dataset Name", "field": "dataset_name", "sortable": True},
            {"name": "symbol", "label": "Symbol", "field": "symbol", "sortable": True},
            {"name": "file_role", "label": "File Role", "field": "file_role"},
            {"name": "status", "label": "Status", "field": "status"},
            {"name": "row_count", "label": "Row Count", "field": "row_count"},
            {"name": "file_size_bytes", "label": "File Size (Bytes)", "field": "file_size_bytes"},
            {"name": "last_refresh_at", "label": "Last Refresh", "field": "last_refresh_at"},
        ]
        manifest_table = ui.table(columns=columns, rows=[]).classes("bq-data-table")
        
    with ui.row().classes("bq-toolbar w-full"):
        ui.button("Refresh", on_click=lambda: load_manifest(dataset_filter.value, status_filter.value, manifest_table))
    
    # Load initial data
    load_manifest("all", "all", manifest_table)


def load_manifest(dataset_filter: str, status_filter: str, table: ui.table):
    """Load manifest data with filters."""
    try:
        where_clauses = []
        params = []
        
        if dataset_filter and dataset_filter != "all":
            where_clauses.append("dataset_name = ?")
            params.append(dataset_filter)
        
        if status_filter and status_filter != "all":
            where_clauses.append("update_status = ?")
            params.append(status_filter)
        
        where_clause = " AND ".join(where_clauses) if where_clauses else "1=1"
        
        sql = f"""
            SELECT dataset_name, symbol, file_role, update_status, row_count, file_size_bytes, last_refresh_at
            FROM data_file_manifest
            WHERE {where_clause}
            ORDER BY dataset_name, symbol, file_role
            LIMIT 100
        """
        
        with get_connection(read_only=True) as conn:
            result = conn.execute(sql, params).fetchall()
        
        data = []
        for row in result:
            data.append({
                "dataset_name": row[0],
                "symbol": row[1],
                "file_role": row[2],
                "status": row[3],
                "row_count": f"{row[4]:,}" if row[4] is not None else "0",
                "file_size_bytes": f"{row[5]:,}" if row[5] is not None else "0",
                "last_refresh_at": str(row[6]) if row[6] is not None else "N/A",
            })
        
        table.rows = data
    except Exception as e:
        LOGGER.log_error(
            "load_manifest",
            type(e).__name__,
            str(e),
            context={"dataset_filter": dataset_filter, "status_filter": status_filter},
            channel="web",
        )
        ui.notify(f"Error loading manifest: {str(e)}", position="top", type="negative")
