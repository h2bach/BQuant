"""SQL Lab page for BQuant Platform."""

from __future__ import annotations

from pathlib import Path
import time

from nicegui import ui
from nicegui.client import Client

from utils.logger import BQuantLogger
from warehouse.duckdb_connection import get_connection

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
LOGGER = BQuantLogger("web_sql_lab", component="web", subcomponent="sql_lab", default_channel="web")


def render_sql_lab(client: Client):
    """Render the SQL Lab page."""
    ui.label("SQL Lab").classes("text-4xl font-bold")
    ui.separator()
    
    with ui.row().classes("w-full"):
        ui.button("Back to Dashboard", on_click=lambda: ui.navigate.to("/"))
    
    ui.separator()
    
    # SQL editor
    with ui.card().classes("w-full"):
        ui.label("SQL Query").classes("text-xl font-semibold")
        sql_editor = ui.textarea(
            value="SELECT * FROM daily_ohlcv_base LIMIT 100",
            placeholder="Enter your SQL query here..."
        ).classes("w-full h-40 font-mono")
    
    ui.separator()
    
    # Results display
    with ui.card().classes("w-full"):
        ui.label("Query Results").classes("text-xl font-semibold")
        results_table = ui.table(columns=[{"name": "result", "label": "result", "field": "result"}], rows=[])
        
    ui.button("Execute Query", on_click=lambda: execute_query(sql_editor.value, results_table))


def execute_query(sql: str, table: ui.table):
    """Execute SQL query and display results."""
    if not sql or not sql.strip():
        ui.notify("Please enter a SQL query", position="top", type="warning")
        return
    
    started = time.perf_counter()
    try:
        LOGGER.log_web_event(
            "Executing SQL Lab query",
            event_type="sql_query_start",
            status="running",
            query_text=sql,
        )
        with get_connection(read_only=True) as conn:
            result = conn.execute(sql).fetchall()
            
            # Get column names
            columns = [desc[0] for desc in conn.description] if conn.description else ["result"]
            table_cols = [{"name": col, "label": col, "field": col, "sortable": True} for col in columns]
            
            # Convert to list of dicts
            data = []
            for row in result:
                row_dict = {}
                for i, value in enumerate(row):
                    col_name = columns[i] if i < len(columns) else f"col_{i}"
                    row_dict[col_name] = str(value) if value is not None else ""
                data.append(row_dict)
            
            # Update table
            table.columns = table_cols
            table.rows = data
            
            LOGGER.log_web_event(
                "SQL Lab query completed",
                event_type="sql_query_complete",
                status="success",
                query_text=sql,
                row_count=len(data),
                duration_seconds=round(time.perf_counter() - started, 3),
            )
            ui.notify(f"Query returned {len(data)} rows", position="top", type="positive")
    except Exception as e:
        LOGGER.log_error(
            "execute_sql_lab_query",
            type(e).__name__,
            str(e),
            context={
                "query_text": sql,
                "duration_seconds": round(time.perf_counter() - started, 3),
            },
            channel="web",
        )
        ui.notify(f"Error executing query: {str(e)}", position="top", type="negative")
