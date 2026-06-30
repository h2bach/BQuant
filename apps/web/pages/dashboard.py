"""Dashboard page for BQuant Platform."""

from __future__ import annotations

from nicegui import ui
from nicegui.client import Client

from apps.web.services.charting import OVERVIEW_PERIOD_OPTIONS, build_market_overview_chart
from utils.logger import BQuantLogger
from warehouse.duckdb_connection import get_connection

LOGGER = BQuantLogger("web_dashboard", component="web", subcomponent="dashboard", default_channel="web")


def render_dashboard(client: Client):
    """Render the main dashboard page.

    Args:
        client: NiceGUI client object supplied by the router. The dashboard
            does not currently use it directly.

    Side Effects:
        Builds NiceGUI widgets, queries dashboard summary data, and executes
        Lightweight Charts JavaScript in the browser.
    """
    del client
    ui.label("BQuant Platform Dashboard").classes("bq-page-title font-bold")
    ui.label("Daily market view for VNIndex and VN30 with core momentum and breadth indicators.").classes(
        "bq-page-subtitle text-sm"
    )
    ui.separator()

    with ui.row().classes("bq-header-row w-full"):
        with ui.row().classes("bq-toolbar"):
            ui.button("Symbol Explorer", on_click=lambda: ui.navigate.to("/symbol"))
            ui.button("Data Catalog", on_click=lambda: ui.navigate.to("/catalog"))
            ui.button("Manifest", on_click=lambda: ui.navigate.to("/manifest"))
            ui.button("Data Quality", on_click=lambda: ui.navigate.to("/quality"))
            ui.button("SQL Lab", on_click=lambda: ui.navigate.to("/sql_lab"))
            ui.button("Operations", on_click=lambda: ui.navigate.to("/operations"))
            ui.button("Alerts", on_click=lambda: ui.navigate.to("/alerts"))
            ui.button("Agents", on_click=lambda: ui.navigate.to("/agents"))
            ui.button("Demo Trading", on_click=lambda: ui.navigate.to("/demo_trading"))
        with ui.row().classes("bq-control-row"):
            ui.label("Range").classes("text-sm text-slate-400")
            period_select = ui.select(
                options=OVERVIEW_PERIOD_OPTIONS,
                value="10Y",
            ).classes("bq-control-field")

    ui.separator()
    ui.label("Market Overview").classes("text-2xl font-semibold")

    overview_container = ui.column().classes("w-full gap-4")

    def render_market_overview() -> None:
        """Rebuild the market overview sections after range changes.

        Side Effects:
            Clears and repopulates `overview_container`, calls chart builders
            for VNIndex and VN30, logs render failures, and executes chart
            JavaScript snippets in the active browser session.
        """
        overview_container.clear()
        try:
            vnindex_chart, vnindex_metrics, vnindex_note = build_market_overview_chart(
                period_select.value or "10Y",
                "VNIndex",
                show_comparison=False,
                height=620,
            )
            vn30_chart, vn30_metrics, vn30_note = build_market_overview_chart(
                period_select.value or "10Y",
                "VN30",
                show_comparison=False,
                height=620,
            )
        except Exception as exc:
            LOGGER.log_error(
                "render_market_overview",
                type(exc).__name__,
                str(exc),
                context={
                    "period_key": period_select.value or "10Y",
                    "dashboard_mode": "split_market_overview",
                },
                channel="web",
            )
            with overview_container:
                with ui.card().classes("w-full"):
                    ui.label(f"Unable to render market overview: {exc}").classes("text-red-400")
            return

        sections = [
            ("VNIndex Overview", "VNIndex Price Structure", vnindex_metrics, vnindex_chart, vnindex_note),
            ("VN30 Overview", "VN30 Price Structure", vn30_metrics, vn30_chart, vn30_note),
        ]

        with overview_container:
            ui.label(
                "VNIndex and VN30 now render with TradingView Lightweight Charts. Price, volume, and signal panes use separate scales so candle structure stays readable."
            ).classes("text-sm text-slate-400")
            for overview_title, chart_title, metrics, chart, note in sections:
                with ui.card().classes("bq-chart-card w-full"):
                    ui.label(overview_title).classes("text-xl font-semibold")
                    with ui.row().classes("bq-card-grid bq-metric-grid w-full mb-2"):
                        for metric in metrics:
                            with ui.column().classes("bq-card rounded border border-slate-700 p-3"):
                                ui.label(metric["label"]).classes("text-xs text-slate-400")
                                ui.label(metric["value"]).classes("text-base font-semibold")
                    ui.label(chart_title).classes("text-lg font-semibold")
                    ui.html(chart.html, sanitize=False).classes("bq-chart-html w-full")
                    ui.run_javascript(chart.script, timeout=5.0)
                    ui.label(note).classes("text-xs text-slate-400")

    period_select.on_value_change(lambda _: render_market_overview())
    render_market_overview()

    ui.separator()
    ui.label("Operational Snapshot").classes("text-2xl font-semibold")
    with ui.row().classes("bq-card-grid w-full"):
        with ui.card().classes("bq-card"):
            ui.label("Dataset Overview").classes("text-xl font-semibold")
            dataset_stats = get_dataset_stats()
            for dataset, count in dataset_stats.items():
                ui.label(f"{dataset}: {count:,} rows")

        with ui.card().classes("bq-card"):
            ui.label("Universe Status").classes("text-xl font-semibold")
            universe_info = get_universe_info()
            ui.label(f"Universe: {universe_info.get('universe_name', 'N/A')}")
            ui.label(f"Symbols: {universe_info.get('symbol_count', 0)}")
            ui.label(f"Effective Date: {universe_info.get('effective_date', 'N/A')}")

        with ui.card().classes("bq-card"):
            ui.label("Pipeline Status").classes("text-xl font-semibold")
            pipeline_info = get_pipeline_status()
            ui.label(f"Last Run: {pipeline_info.get('last_run', 'N/A')}")
            ui.label(f"Status: {pipeline_info.get('status', 'N/A')}")
            ui.label(f"Success Rate: {pipeline_info.get('success_rate', 'N/A')}")


def get_dataset_stats() -> dict[str, int]:
    """Get dataset row counts for the dashboard snapshot.

    Returns:
        Mapping from user-facing dataset label to row count. If DuckDB is not
        readable, returns the counts collected before the error, often empty.

    Side Effects:
        Opens a read-only DuckDB connection and logs query failures.
    """
    stats = {}
    try:
        with get_connection(read_only=True) as conn:
            stats["Daily OHLCV Base"] = conn.execute("SELECT COUNT(*) FROM daily_ohlcv_base").fetchone()[0]
            stats["Intraday 15m Base"] = conn.execute("SELECT COUNT(*) FROM intraday_ohlcv_15m_base").fetchone()[0]
            stats["Universe Members"] = conn.execute("SELECT COUNT(*) FROM universe_members").fetchone()[0]
    except Exception as exc:
        LOGGER.log_error("get_dataset_stats", type(exc).__name__, str(exc), context={}, channel="web")
    return stats


def get_universe_info() -> dict[str, str | int]:
    """Get current universe metadata for the dashboard snapshot.

    Returns:
        Dictionary containing `universe_name`, `symbol_count`, and
        `effective_date` when available.

    Side Effects:
        Opens a read-only DuckDB connection and logs query failures.
    """
    info = {}
    try:
        with get_connection(read_only=True) as conn:
            result = conn.execute(
                """
                SELECT universe_name, COUNT(DISTINCT symbol) as symbol_count,
                       MIN(effective_date) as effective_date
                FROM universe_members
                GROUP BY universe_name
                """
            ).fetchone()
            if result:
                info["universe_name"] = result[0]
                info["symbol_count"] = result[1]
                info["effective_date"] = str(result[2])
    except Exception as exc:
        LOGGER.log_error("get_universe_info", type(exc).__name__, str(exc), context={}, channel="web")
    return info


def get_pipeline_status() -> dict[str, str]:
    """Get latest pipeline run status for the dashboard snapshot.

    Returns:
        Dictionary containing `last_run`, `success_rate`, and latest `status`
        when pipeline history exists.

    Side Effects:
        Opens a read-only DuckDB connection and logs query failures.
    """
    info = {}
    try:
        with get_connection(read_only=True) as conn:
            result = conn.execute(
                """
                SELECT MAX(end_time) as last_run,
                       SUM(CASE WHEN status = 'success' THEN 1 ELSE 0 END) * 100.0 / COUNT(*) as success_rate,
                       (SELECT status FROM pipeline_runs ORDER BY end_time DESC LIMIT 1) as status
                FROM pipeline_runs
                """
            ).fetchone()
            if result:
                info["last_run"] = str(result[0]) if result[0] else "N/A"
                info["success_rate"] = f"{result[1]:.1f}%" if result[1] else "N/A"
                info["status"] = result[2] if result[2] else "N/A"
    except Exception as exc:
        LOGGER.log_error("get_pipeline_status", type(exc).__name__, str(exc), context={}, channel="web")
    return info
