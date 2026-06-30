"""Symbol explorer page for BQuant Platform."""

from __future__ import annotations

from nicegui import ui
from nicegui.client import Client

from apps.web.services.charting import (
    SYMBOL_DAILY_RANGE_OPTIONS,
    SYMBOL_INTRADAY_RANGE_OPTIONS,
    build_symbol_chart,
)
from utils.logger import BQuantLogger
from warehouse.duckdb_connection import get_connection

LOGGER = BQuantLogger("web_symbol_explorer", component="web", subcomponent="symbol_explorer", default_channel="web")


def render_symbol_explorer(client: Client):
    """Render the Symbol Explorer page.

    Args:
        client: NiceGUI client object supplied by the router. It is not used
            directly because state is stored in page widgets.

    Side Effects:
        Creates symbol/mode/range controls, renders chart/table widgets, and
        executes Lightweight Charts JavaScript in the browser.
    """
    del client
    symbols = get_universe_symbols()

    ui.label("Symbol Explorer").classes("bq-page-title font-bold")
    ui.label("Daily and intraday chart workspace for VN30 constituents.").classes("bq-page-subtitle text-sm")
    ui.separator()

    with ui.row().classes("bq-header-row w-full"):
        ui.button("Back to Dashboard", on_click=lambda: ui.navigate.to("/"))
        with ui.row().classes("bq-control-row"):
            symbol_select = ui.select(options=symbols, value=symbols[0] if symbols else None).classes("bq-control-field")
            mode_select = ui.select(options=["daily", "intraday"], value="daily").classes("bq-control-field")
            range_select = ui.select(options=SYMBOL_DAILY_RANGE_OPTIONS, value="10Y").classes("bq-control-field")
            ui.button("Load", on_click=lambda: render_symbol_workspace())

    ui.separator()
    metrics_container = ui.row().classes("bq-card-grid bq-metric-grid w-full")
    note_container = ui.column().classes("w-full")
    chart_container = ui.column().classes("w-full")

    with ui.card().classes("bq-table-card w-full"):
        ui.label("OHLCV Data").classes("text-xl font-semibold")
        table = ui.table(
            columns=[],
            rows=[],
            pagination={"rowsPerPage": 15},
        ).props(':rows-per-page-options="[15, 20, 50]"').classes("bq-data-table")

    def sync_range_options() -> None:
        """Swap range presets when daily/intraday mode changes.

        Side Effects:
            Mutates `range_select.options` and `range_select.value` so the UI
            never keeps an invalid period key for the selected mode.
        """
        options = SYMBOL_DAILY_RANGE_OPTIONS if mode_select.value == "daily" else SYMBOL_INTRADAY_RANGE_OPTIONS
        default_value = "10Y" if mode_select.value == "daily" else "20D"
        range_select.options = options
        if range_select.value not in options:
            range_select.value = default_value
        range_select.update()

    def render_symbol_workspace() -> None:
        """Render metrics, chart, and OHLCV table for the selected state.

        Side Effects:
            Clears and repopulates metric/note/chart containers, updates the
            OHLCV table rows, emits UI notifications for invalid input, logs
            chart build failures, and executes chart JavaScript.
        """
        symbol = symbol_select.value
        mode = mode_select.value or "daily"
        period_key = range_select.value or ("10Y" if mode == "daily" else "20D")

        if not symbol:
            ui.notify("Please select a symbol first", position="top", type="warning")
            return

        metrics_container.clear()
        note_container.clear()
        chart_container.clear()

        try:
            chart, metrics, note = build_symbol_chart(symbol, mode=mode, period_key=period_key)
        except Exception as exc:
            LOGGER.log_error(
                "render_symbol_workspace",
                type(exc).__name__,
                str(exc),
                context={"symbol": symbol, "mode": mode, "period_key": period_key},
                channel="web",
            )
            with chart_container:
                with ui.card().classes("w-full"):
                    ui.label(f"Unable to render {symbol}: {exc}").classes("text-red-400")
            table.columns = []
            table.rows = []
            table.update()
            return

        with metrics_container:
            for metric in metrics:
                with ui.card().classes("bq-card"):
                    ui.label(metric["label"]).classes("text-sm text-slate-400")
                    ui.label(metric["value"]).classes("text-xl font-semibold")

        with note_container:
            ui.label(note).classes("text-sm text-slate-400")

        with chart_container:
            with ui.card().classes("bq-chart-card w-full"):
                ui.html(chart.html, sanitize=False).classes("bq-chart-html w-full")
                ui.run_javascript(chart.script, timeout=5.0)

        table.columns = build_table_columns(mode)
        table.rows = load_symbol_rows(symbol, mode, period_key)
        table.update()

    mode_select.on_value_change(lambda _: (sync_range_options(), render_symbol_workspace()))
    symbol_select.on_value_change(lambda _: render_symbol_workspace())
    range_select.on_value_change(lambda _: render_symbol_workspace())

    sync_range_options()
    if symbols:
        render_symbol_workspace()


def get_universe_symbols() -> list[str]:
    """Get the sorted list of symbols in the active universe.

    Returns:
        List of distinct universe symbols. Returns an empty list when the
        warehouse query fails.

    Side Effects:
        Opens a read-only DuckDB connection and logs query failures.
    """
    symbols = []
    try:
        with get_connection(read_only=True) as conn:
            result = conn.execute("SELECT DISTINCT symbol FROM universe_members ORDER BY symbol").fetchall()
            symbols = [row[0] for row in result if row and row[0]]
    except Exception as exc:
        LOGGER.log_error("get_universe_symbols", type(exc).__name__, str(exc), context={}, channel="web")
    return symbols


def build_table_columns(mode: str) -> list[dict[str, str | bool]]:
    """Build OHLCV table column definitions for the selected mode.

    Args:
        mode: `daily` or `intraday`. Determines whether the timestamp column
            is `trading_date` or `bar_time`.

    Returns:
        NiceGUI table column dictionaries for timestamp, OHLC, and volume.
    """
    timestamp_field = "trading_date" if mode == "daily" else "bar_time"
    timestamp_label = "Trading Date" if mode == "daily" else "Bar Time"
    return [
        {"name": timestamp_field, "label": timestamp_label, "field": timestamp_field, "sortable": True},
        {"name": "open", "label": "Open", "field": "open"},
        {"name": "high", "label": "High", "field": "high"},
        {"name": "low", "label": "Low", "field": "low"},
        {"name": "close", "label": "Close", "field": "close"},
        {"name": "volume", "label": "Volume", "field": "volume"},
    ]


def _row_limit(mode: str, period_key: str) -> int:
    """Return a table row budget aligned with the active chart range.

    Args:
        mode: `daily` or `intraday` data mode.
        period_key: Active UI range preset.

    Returns:
        Maximum number of rows to fetch for the visible OHLCV table.
    """
    daily_limits = {
        "3M": 70,
        "6M": 140,
        "1Y": 260,
        "3Y": 780,
        "5Y": 1300,
        "10Y": 2600,
        "ALL": 5000,
    }
    intraday_limits = {
        "5D": 140,
        "20D": 560,
        "60D": 1700,
    }
    mapping = daily_limits if mode == "daily" else intraday_limits
    return mapping.get(period_key, 260 if mode == "daily" else 560)


def load_symbol_rows(symbol: str, mode: str, period_key: str) -> list[dict[str, str]]:
    """Load OHLCV table rows for the selected symbol and mode.

    Args:
        symbol: VN30 constituent ticker selected in the UI.
        mode: `daily` reads `daily_ohlcv_base`; `intraday` reads
            `intraday_ohlcv_15m_base`.
        period_key: Active UI range preset used only to cap table row count.

    Returns:
        List of table-row dictionaries with formatted timestamp, OHLC, and
        volume strings.

    Side Effects:
        Opens a read-only DuckDB connection, logs query failures, and shows a
        NiceGUI notification when table loading fails.
    """
    query = (
        """
        SELECT trading_date AS event_time, open, high, low, close, volume
        FROM daily_ohlcv_base
        WHERE symbol = ?
        ORDER BY trading_date DESC
        LIMIT ?
        """
        if mode == "daily"
        else """
        SELECT bar_time AS event_time, open, high, low, close, volume
        FROM intraday_ohlcv_15m_base
        WHERE symbol = ?
        ORDER BY bar_time DESC
        LIMIT ?
        """
    )

    rows: list[dict[str, str]] = []
    try:
        with get_connection(read_only=True) as conn:
            result = conn.execute(query, [symbol, _row_limit(mode, period_key)]).fetchall()
        key = "trading_date" if mode == "daily" else "bar_time"
        for event_time, open_price, high_price, low_price, close_price, volume in result:
            volume_text = "0" if volume is None else f"{int(round(volume)):,}"
            rows.append(
                {
                    key: str(event_time),
                    "open": f"{open_price:.2f}" if open_price is not None else "N/A",
                    "high": f"{high_price:.2f}" if high_price is not None else "N/A",
                    "low": f"{low_price:.2f}" if low_price is not None else "N/A",
                    "close": f"{close_price:.2f}" if close_price is not None else "N/A",
                    "volume": volume_text,
                }
            )
    except Exception as exc:
        LOGGER.log_error(
            "load_symbol_rows",
            type(exc).__name__,
            str(exc),
            context={"symbol": symbol, "mode": mode, "period_key": period_key},
            channel="web",
        )
        ui.notify(f"Error loading table data: {exc}", position="top", type="negative")
    return rows
