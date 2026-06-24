"""Alerts page for BQuant observability."""

from __future__ import annotations

from nicegui import ui
from nicegui.client import Client

from apps.web.services.operations import alert_keys, load_alerts, trigger_action, trigger_alert_transition
from utils.logger import BQuantLogger


LOGGER = BQuantLogger("web_alerts_page", component="web", subcomponent="alerts_page", default_channel="web")


def render_alerts(client: Client):
    del client
    ui.label("Alerts").classes("text-4xl font-bold")
    ui.label("Active and historical observability alerts with acknowledgement controls.").classes(
        "text-sm text-slate-400"
    )
    ui.separator()

    with ui.row().classes("w-full items-center gap-2"):
        ui.button("Back to Dashboard", on_click=lambda: ui.navigate.to("/"))
        ui.button("Operations", on_click=lambda: ui.navigate.to("/operations"))

    with ui.row().classes("w-full items-end gap-3"):
        status_select = ui.select(options=["all", "open", "acknowledged", "resolved"], value="open").classes("w-40")
        severity_select = ui.select(options=["all", "CRITICAL", "ERROR", "WARNING", "INFO"], value="all").classes("w-40")
        dataset_input = ui.input(label="Dataset", placeholder="e.g. intraday_ohlcv_15m_delta").classes("w-72")
        symbol_input = ui.input(label="Symbol", placeholder="e.g. FPT").classes("w-40")
        ui.button("Refresh", on_click=lambda: render_table())
        ui.button("Re-evaluate Alerts", on_click=lambda: _trigger_eval())

    ui.separator()

    selected_alert = ui.select(options=[], label="Selected Alert").classes("w-full")
    with ui.row().classes("w-full gap-2"):
        ui.button("Acknowledge", on_click=lambda: _transition("acknowledged"))
        ui.button("Resolve", on_click=lambda: _transition("resolved"))

    table = ui.table(
        columns=[
            {"name": "alert_key", "label": "Alert Key", "field": "alert_key"},
            {"name": "alert_type", "label": "Type", "field": "alert_type"},
            {"name": "severity", "label": "Severity", "field": "severity"},
            {"name": "dataset_name", "label": "Dataset", "field": "dataset_name"},
            {"name": "symbol", "label": "Symbol", "field": "symbol"},
            {"name": "status", "label": "Status", "field": "status"},
            {"name": "message", "label": "Message", "field": "message"},
            {"name": "first_event_ts", "label": "First Seen", "field": "first_event_ts"},
            {"name": "last_event_ts", "label": "Last Seen", "field": "last_event_ts"},
        ],
        rows=[],
        pagination={"rowsPerPage": 12},
    ).classes("w-full")

    def _trigger_eval() -> None:
        try:
            pid = trigger_action("evaluate_alerts")
            ui.notify(f"evaluate_alerts submitted (pid={pid})", type="positive", position="top")
        except Exception as exc:
            LOGGER.log_error(
                "alerts_trigger_eval",
                type(exc).__name__,
                str(exc),
                context={},
                channel="web",
            )
            ui.notify(f"Failed to trigger alert evaluation: {exc}", type="negative", position="top")

    def _transition(target_status: str) -> None:
        if not selected_alert.value:
            ui.notify("Select an alert key first", type="warning", position="top")
            return
        try:
            pid = trigger_alert_transition(str(selected_alert.value), target_status)
            ui.notify(f"{target_status} submitted (pid={pid})", type="positive", position="top")
        except Exception as exc:
            LOGGER.log_error(
                "alerts_transition",
                type(exc).__name__,
                str(exc),
                context={"alert_key": selected_alert.value, "target_status": target_status},
                channel="web",
            )
            ui.notify(f"Failed to change alert status: {exc}", type="negative", position="top")

    def render_table() -> None:
        try:
            rows = load_alerts(
                status_filter=str(status_select.value or "all"),
                severity_filter=str(severity_select.value or "all"),
                dataset_filter=str(dataset_input.value or "all").strip() or "all",
                symbol_filter=str(symbol_input.value or ""),
            )
            table.rows = rows
            table.update()
            selected_alert.options = alert_keys("open")
            selected_alert.update()
        except Exception as exc:
            LOGGER.log_error(
                "load_alerts",
                type(exc).__name__,
                str(exc),
                context={},
                channel="web",
            )
            ui.notify(f"Unable to load alerts: {exc}", type="negative", position="top")

    status_select.on_value_change(lambda _: render_table())
    severity_select.on_value_change(lambda _: render_table())
    render_table()
