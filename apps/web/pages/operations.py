"""Operations monitoring page for BQuant."""

from __future__ import annotations

from nicegui import ui
from nicegui.client import Client

from apps.web.services.operations import load_operations_snapshot, trigger_action
from utils.logger import BQuantLogger


LOGGER = BQuantLogger("web_operations_page", component="web", subcomponent="operations_page", default_channel="web")


def render_operations(client: Client):
    """Render the operations dashboard and safe manual control actions."""
    del client
    ui.label("Operations").classes("text-4xl font-bold")
    ui.label("Live update health, recent jobs, checkpoints, and safe control actions.").classes(
        "text-sm text-slate-400"
    )
    ui.separator()

    with ui.row().classes("w-full items-center gap-2"):
        ui.button("Back to Dashboard", on_click=lambda: ui.navigate.to("/"))
        ui.button("Alerts", on_click=lambda: ui.navigate.to("/alerts"))
        ui.button("Refresh View", on_click=lambda: render_snapshot())

    with ui.row().classes("w-full gap-2"):
        ui.button("Run Intraday Delta", on_click=lambda: _trigger("run_intraday_delta"))
        ui.button("Run EOD Reconcile", on_click=lambda: _trigger("run_eod_reconcile"))
        ui.button("Run dbt Transforms", on_click=lambda: _trigger("run_dbt_transforms"))
        ui.button("Run Agent Cycle", on_click=lambda: _trigger("run_agent_cycle"))
        ui.button("Re-ingest Logs", on_click=lambda: _trigger("ingest_observability_logs"))
        ui.button("Refresh Manifest", on_click=lambda: _trigger("refresh_manifest"))
        ui.button("Re-evaluate Alerts", on_click=lambda: _trigger("evaluate_alerts"))

    ui.separator()

    summary_container = ui.row().classes("w-full gap-4")
    refresh_container = ui.row().classes("w-full gap-4")
    source_container = ui.card().classes("w-full")
    checkpoint_container = ui.card().classes("w-full")
    jobs_container = ui.card().classes("w-full")
    errors_container = ui.card().classes("w-full")

    def _trigger(action_name: str) -> None:
        """Submit an allowed maintenance action through the operations service."""
        try:
            pid = trigger_action(action_name)
            ui.notify(f"{action_name} submitted (pid={pid})", type="positive", position="top")
        except Exception as exc:
            LOGGER.log_error(
                "operations_trigger",
                type(exc).__name__,
                str(exc),
                context={"action_name": action_name},
                channel="web",
            )
            ui.notify(f"Failed to trigger {action_name}: {exc}", type="negative", position="top")

    def render_snapshot() -> None:
        """Reload the operations snapshot and redraw all monitoring tables/cards."""
        summary_container.clear()
        refresh_container.clear()
        source_container.clear()
        checkpoint_container.clear()
        jobs_container.clear()
        errors_container.clear()

        try:
            snapshot = load_operations_snapshot()
        except Exception as exc:
            LOGGER.log_error(
                "load_operations_snapshot",
                type(exc).__name__,
                str(exc),
                context={},
                channel="web",
            )
            ui.notify(f"Unable to load operations snapshot: {exc}", type="negative", position="top")
            return

        with summary_container:
            cards = [
                ("Market State", snapshot["market_session_state"]),
                ("Current Time", snapshot["current_time"]),
                ("Stale Symbols", str(snapshot["stale_symbol_count"])),
                ("Failed Jobs 24h", str(snapshot["failed_jobs_24h"])),
                ("Worker Heartbeat", snapshot.get("worker_health", {}).get("last_heartbeat_ts", "N/A")),
                ("Worker Session", snapshot.get("worker_health", {}).get("last_session_state", "N/A")),
            ]
            for label, value in cards:
                with ui.card().classes("min-w-[190px] flex-1"):
                    ui.label(label).classes("text-sm text-slate-400")
                    ui.label(value).classes("text-lg font-semibold")

        with refresh_container:
            for state in snapshot["refresh_states"]:
                with ui.card().classes("min-w-[240px] flex-1"):
                    ui.label(state["dataset_name"]).classes("text-sm text-slate-400")
                    ui.label(f"Version {state['refresh_version']}").classes("text-xl font-semibold")
                    ui.label(f"Last Success: {state['last_success_at'] or 'N/A'}")
                    ui.label(f"Latest Data: {state['latest_data_ts'] or 'N/A'}")

        with source_container:
            ui.label("Source Lag Summary").classes("text-xl font-semibold")
            ui.table(
                columns=[
                    {"name": "symbol", "label": "Symbol", "field": "symbol"},
                    {"name": "provider", "label": "Provider", "field": "provider"},
                    {"name": "last_request_end", "label": "Last Request End", "field": "last_request_end"},
                    {"name": "status", "label": "Status", "field": "status"},
                    {"name": "rows_fetched", "label": "Rows", "field": "rows_fetched"},
                ],
                rows=snapshot["source_lag_summary"],
                pagination={"rowsPerPage": 10},
            )

        with checkpoint_container:
            ui.label("Live Checkpoints").classes("text-xl font-semibold")
            ui.table(
                columns=[
                    {"name": "dataset_name", "label": "Dataset", "field": "dataset_name"},
                    {"name": "symbol", "label": "Symbol", "field": "symbol"},
                    {"name": "watermark_ts", "label": "Watermark", "field": "watermark_ts"},
                    {"name": "updated_at", "label": "Updated", "field": "updated_at"},
                ],
                rows=snapshot["checkpoints"],
                pagination={"rowsPerPage": 15},
            )

        with jobs_container:
            ui.label("Recent Jobs").classes("text-xl font-semibold")
            ui.table(
                columns=[
                    {"name": "pipeline_name", "label": "Pipeline", "field": "pipeline_name"},
                    {"name": "status", "label": "Status", "field": "status"},
                    {"name": "trigger_type", "label": "Trigger", "field": "trigger_type"},
                    {"name": "start_time", "label": "Start", "field": "start_time"},
                    {"name": "end_time", "label": "End", "field": "end_time"},
                    {"name": "duration_seconds", "label": "Duration (s)", "field": "duration_seconds"},
                    {"name": "dataset_name", "label": "Dataset", "field": "dataset_name"},
                    {"name": "error_message", "label": "Error", "field": "error_message"},
                ],
                rows=snapshot["recent_jobs"],
                pagination={"rowsPerPage": 10},
            )

        with errors_container:
            ui.label("Recent Error Events").classes("text-xl font-semibold")
            ui.table(
                columns=[
                    {"name": "event_ts", "label": "Time", "field": "event_ts"},
                    {"name": "component", "label": "Component", "field": "component"},
                    {"name": "subcomponent", "label": "Subcomponent", "field": "subcomponent"},
                    {"name": "event_type", "label": "Event Type", "field": "event_type"},
                    {"name": "dataset_name", "label": "Dataset", "field": "dataset_name"},
                    {"name": "symbol", "label": "Symbol", "field": "symbol"},
                    {"name": "message", "label": "Message", "field": "message"},
                ],
                rows=snapshot["recent_errors"],
                pagination={"rowsPerPage": 10},
            )

    render_snapshot()
