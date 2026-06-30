"""Data quality page for BQuant Platform."""

from __future__ import annotations

from pathlib import Path

from nicegui import ui
from nicegui.client import Client

from utils.logger import BQuantLogger
from warehouse.duckdb_connection import get_connection

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
LOGGER = BQuantLogger("web_quality", component="web", subcomponent="quality", default_channel="web")


def render_quality(client: Client):
    """Render the data quality page."""
    del client
    ui.label("Data Quality").classes("bq-page-title font-bold")
    ui.separator()
    
    with ui.row().classes("bq-toolbar w-full"):
        ui.button("Back to Dashboard", on_click=lambda: ui.navigate.to("/"))
    
    ui.separator()
    
    # Quality checks summary
    with ui.row().classes("bq-card-grid w-full"):
        with ui.card().classes("bq-card"):
            ui.label("Daily OHLCV Quality").classes("text-xl font-semibold")
            daily_quality = get_daily_ohlcv_quality()
            ui.label(f"Total Rows: {daily_quality.get('total_rows', 0):,}")
            ui.label(f"Missing Values: {daily_quality.get('missing_values', 0):,}")
            ui.label(f"Invalid OHLC: {daily_quality.get('invalid_ohlc', 0):,}")
            ui.label(f"Quality Score: {daily_quality.get('quality_score', 0):.1f}%")
        
        with ui.card().classes("bq-card"):
            ui.label("Intraday OHLCV Quality").classes("text-xl font-semibold")
            intraday_quality = get_intraday_ohlcv_quality()
            ui.label(f"Total Rows: {intraday_quality.get('total_rows', 0):,}")
            ui.label(f"Missing Values: {intraday_quality.get('missing_values', 0):,}")
            ui.label(f"Invalid OHLC: {intraday_quality.get('invalid_ohlc', 0):,}")
            ui.label(f"Quality Score: {intraday_quality.get('quality_score', 0):.1f}%")
        
        with ui.card().classes("bq-card"):
            ui.label("Data Freshness").classes("text-xl font-semibold")
            freshness = get_data_freshness()
            ui.label(f"Daily Latest: {freshness.get('daily_latest', 'N/A')}")
            ui.label(f"Intraday Latest: {freshness.get('intraday_latest', 'N/A')}")
            ui.label(f"Stale Datasets: {freshness.get('stale_count', 0)}")
            ui.label(f"Status: {freshness.get('overall_status', 'N/A')}")
    
    ui.separator()
    
    # Quality issues table
    with ui.card().classes("bq-table-card w-full"):
        ui.label("Recent Quality Issues").classes("text-xl font-semibold")
        columns = [
            {"name": "table", "label": "Table", "field": "table", "sortable": True},
            {"name": "issue_type", "label": "Issue Type", "field": "issue_type", "sortable": True},
            {"name": "symbol", "label": "Symbol", "field": "symbol"},
            {"name": "count", "label": "Count", "field": "count"},
            {"name": "last_seen", "label": "Last Seen", "field": "last_seen"},
        ]
        issues_table = ui.table(columns=columns, rows=[]).classes("bq-data-table")
    
    # Load quality issues
    load_quality_issues(issues_table)


def get_daily_ohlcv_quality() -> dict[str, int | float]:
    """Get daily OHLCV quality metrics."""
    quality = {}
    try:
        with get_connection(read_only=True) as conn:
            total_rows = conn.execute("SELECT COUNT(*) FROM daily_ohlcv_base").fetchone()[0]
            quality["total_rows"] = total_rows
            
            # Check for missing values
            missing = conn.execute("""
                SELECT COUNT(*) FROM daily_ohlcv_base 
                WHERE open IS NULL OR high IS NULL OR low IS NULL OR close IS NULL OR volume IS NULL
            """).fetchone()[0]
            quality["missing_values"] = missing
            
            # Check for invalid OHLC (high < low, etc.)
            invalid = conn.execute("""
                SELECT COUNT(*) FROM daily_ohlcv_base 
                WHERE high < low OR open < 0 OR close < 0 OR volume < 0
            """).fetchone()[0]
            quality["invalid_ohlc"] = invalid
            
            # Calculate quality score
            if total_rows > 0:
                quality_score = ((total_rows - missing - invalid) / total_rows) * 100
                quality["quality_score"] = quality_score
            else:
                quality["quality_score"] = 0.0
    except Exception as e:
        LOGGER.log_error("get_daily_ohlcv_quality", type(e).__name__, str(e), context={}, channel="web")
    return quality


def get_intraday_ohlcv_quality() -> dict[str, int | float]:
    """Get intraday OHLCV quality metrics."""
    quality = {}
    try:
        with get_connection(read_only=True) as conn:
            total_rows = conn.execute("SELECT COUNT(*) FROM intraday_ohlcv_15m_base").fetchone()[0]
            quality["total_rows"] = total_rows
            
            # Check for missing values
            missing = conn.execute("""
                SELECT COUNT(*) FROM intraday_ohlcv_15m_base 
                WHERE open IS NULL OR high IS NULL OR low IS NULL OR close IS NULL OR volume IS NULL
            """).fetchone()[0]
            quality["missing_values"] = missing
            
            # Check for invalid OHLC
            invalid = conn.execute("""
                SELECT COUNT(*) FROM intraday_ohlcv_15m_base 
                WHERE high < low OR open < 0 OR close < 0 OR volume < 0
            """).fetchone()[0]
            quality["invalid_ohlc"] = invalid
            
            # Calculate quality score
            if total_rows > 0:
                quality_score = ((total_rows - missing - invalid) / total_rows) * 100
                quality["quality_score"] = quality_score
            else:
                quality["quality_score"] = 0.0
    except Exception as e:
        LOGGER.log_error("get_intraday_ohlcv_quality", type(e).__name__, str(e), context={}, channel="web")
    return quality


def get_data_freshness() -> dict[str, str | int]:
    """Get data freshness information."""
    freshness = {}
    try:
        with get_connection(read_only=True) as conn:
            # Get latest daily date
            daily_latest = conn.execute("SELECT MAX(trading_date) FROM daily_ohlcv_base").fetchone()[0]
            freshness["daily_latest"] = str(daily_latest) if daily_latest else "N/A"
            
            # Get latest intraday time
            intraday_latest = conn.execute("SELECT MAX(bar_time) FROM intraday_ohlcv_15m_base").fetchone()[0]
            freshness["intraday_latest"] = str(intraday_latest) if intraday_latest else "N/A"
            
            # Count stale datasets from manifest
            stale_count = conn.execute("""
                SELECT COUNT(*) FROM data_file_manifest 
                WHERE update_status = 'stale'
            """).fetchone()[0]
            freshness["stale_count"] = stale_count
            
            # Overall status
            if stale_count == 0:
                freshness["overall_status"] = "Good"
            elif stale_count < 5:
                freshness["overall_status"] = "Warning"
            else:
                freshness["overall_status"] = "Critical"
    except Exception as e:
        LOGGER.log_error("get_data_freshness", type(e).__name__, str(e), context={}, channel="web")
    return freshness


def load_quality_issues(table: ui.table):
    """Load recent quality issues."""
    try:
        with get_connection(read_only=True) as conn:
            # Get quality issues from manifest
            result = conn.execute("""
                SELECT dataset_name as table, update_status as issue_type, symbol, row_count as count, last_refresh_at as last_seen
                FROM data_file_manifest
                WHERE update_status IN ('stale', 'awaiting_refresh_window', 'pending_merge')
                ORDER BY last_refresh_at DESC
                LIMIT 20
            """).fetchall()
            
            data = []
            for row in result:
                data.append({
                    "table": row[0],
                    "issue_type": row[1],
                    "symbol": row[2],
                    "count": f"{row[3]:,}" if row[3] is not None else "0",
                    "last_seen": str(row[4]) if row[4] is not None else "N/A",
                })
            
            table.rows = data
    except Exception as e:
        LOGGER.log_error("load_quality_issues", type(e).__name__, str(e), context={}, channel="web")
