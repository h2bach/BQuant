"""Agentic AI workspace for BQuant system analysis and chat."""

from __future__ import annotations

from nicegui import ui
from nicegui.client import Client

from agents.system_analysis import answer_latest_system_question, load_latest_system_analysis
from apps.web.services.operations import trigger_action
from utils.logger import BQuantLogger


LOGGER = BQuantLogger("web_agents_page", component="web", subcomponent="agents_page", default_channel="web")


def render_agents(client: Client):
    """Render BQuant's agentic AI control and report workspace.

    Args:
        client: NiceGUI client object supplied by the router. It is currently
            unused because the page stores state in widgets.

    Side Effects:
        Creates manual action buttons, loads the latest system-analysis report,
        and answers user questions from the stored deterministic analysis.
    """
    del client
    ui.label("BQuant Agentic AI").classes("text-4xl font-bold")
    ui.label("Data health, chart/TA analysis, and portfolio recommendation agents.").classes(
        "text-sm text-slate-400"
    )
    ui.separator()

    with ui.row().classes("w-full items-center gap-2"):
        ui.button("Back to Dashboard", on_click=lambda: ui.navigate.to("/"))
        ui.button("Operations", on_click=lambda: ui.navigate.to("/operations"))
        ui.button("Alerts", on_click=lambda: ui.navigate.to("/alerts"))
        ui.button("Refresh View", on_click=lambda: render_latest_report())

    with ui.row().classes("w-full gap-2"):
        ui.button("Auto Update Data", on_click=lambda: _trigger("auto_update_data"))
        ui.button("Run System Analysis", on_click=lambda: _trigger("run_agent_system_analysis"))
        ui.button("Run Recommendation Cycle", on_click=lambda: _trigger("run_agent_cycle"))

    ui.separator()

    summary_container = ui.row().classes("w-full gap-4")
    report_container = ui.card().classes("w-full")
    chat_container = ui.card().classes("w-full")

    def _trigger(action_name: str) -> None:
        """Submit one supported agent/operations action."""
        try:
            pid = trigger_action(action_name)
            ui.notify(f"{action_name} submitted (pid={pid})", type="positive", position="top")
        except Exception as exc:
            LOGGER.log_error(
                "agents_trigger",
                type(exc).__name__,
                str(exc),
                context={"action_name": action_name},
                channel="web",
            )
            ui.notify(f"Failed to trigger {action_name}: {exc}", type="negative", position="top")

    def render_latest_report() -> None:
        """Reload the latest persisted system-analysis report."""
        summary_container.clear()
        report_container.clear()
        chat_container.clear()
        try:
            latest = load_latest_system_analysis()
        except Exception as exc:
            LOGGER.log_error(
                "load_latest_system_analysis",
                type(exc).__name__,
                str(exc),
                context={},
                channel="web",
            )
            with report_container:
                ui.label(f"Unable to load agent analysis: {exc}").classes("text-red-400")
            return

        if latest is None:
            with report_container:
                ui.label("No system analysis has been generated yet.").classes("text-xl font-semibold")
                ui.label("Run System Analysis to create the first report.").classes("text-sm text-slate-400")
            return

        sections = latest["sections"]
        data_health = sections.get("data_health", {})
        market = sections.get("chart_ta", {})
        portfolio = sections.get("portfolio", {})
        llm_runtime = sections.get("llm_runtime", {})

        with summary_container:
            cards = [
                ("Run ID", str(latest["run_id"])[:8]),
                ("Analysis Date", str(latest["analysis_date"])),
                ("Data Health", str(data_health.get("status", "N/A"))),
                ("Market Regime", str(market.get("market_regime", "N/A"))),
                ("Portfolio", str(portfolio.get("status", "N/A"))),
                ("LLM Enabled", str(llm_runtime.get("enabled", False))),
            ]
            for label, value in cards:
                with ui.card().classes("min-w-[170px] flex-1"):
                    ui.label(label).classes("text-sm text-slate-400")
                    ui.label(value).classes("text-lg font-semibold")

        with report_container:
            ui.label("Latest Agent Report").classes("text-xl font-semibold")
            ui.label(f"Created at: {latest['created_at']}").classes("text-xs text-slate-400")
            ui.markdown(str(latest["report_markdown"])).classes("w-full")

        with chat_container:
            ui.label("Ask BQuant").classes("text-xl font-semibold")
            ui.label("This v1 chat reads the latest deterministic report. LLM-backed explanation can be enabled later.").classes(
                "text-sm text-slate-400"
            )
            question = ui.textarea(label="Question", placeholder="Ask about data freshness, VNIndex/VN30 trend, or portfolio allocation.").classes(
                "w-full"
            )
            answer_box = ui.markdown("").classes("w-full")

            def answer_question() -> None:
                """Answer the user question from the latest stored analysis."""
                if not str(question.value or "").strip():
                    ui.notify("Enter a question first", type="warning", position="top")
                    return
                try:
                    answer_box.set_content(answer_latest_system_question(str(question.value)))
                    answer_box.update()
                    LOGGER.log_web_event(
                        "Answered BQuant agent question",
                        event_type="agent_chat",
                        status="success",
                        question=str(question.value),
                    )
                except Exception as exc:
                    LOGGER.log_error(
                        "answer_agent_question",
                        type(exc).__name__,
                        str(exc),
                        context={"question": str(question.value)},
                        channel="web",
                    )
                    ui.notify(f"Unable to answer question: {exc}", type="negative", position="top")

            ui.button("Ask", on_click=answer_question)

    render_latest_report()
