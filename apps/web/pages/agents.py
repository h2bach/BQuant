"""Agentic AI workspace for BQuant system analysis and chat."""

from __future__ import annotations

from nicegui import ui
from nicegui.client import Client

from agents.system_analysis import (
    answer_live_system_question,
    load_latest_system_analysis,
    load_recent_agent_chat_messages,
)
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
        and answers user questions with fresh context plus a local LLM.
    """
    del client
    ui.label("BQuant Agentic AI").classes("bq-page-title font-bold")
    ui.label("Data health, chart/TA analysis, and portfolio recommendation agents.").classes(
        "bq-page-subtitle text-sm"
    )
    ui.separator()

    with ui.row().classes("bq-toolbar w-full"):
        ui.button("Back to Dashboard", on_click=lambda: ui.navigate.to("/"))
        ui.button("Operations", on_click=lambda: ui.navigate.to("/operations"))
        ui.button("Alerts", on_click=lambda: ui.navigate.to("/alerts"))
        ui.button("Demo Trading", on_click=lambda: ui.navigate.to("/demo_trading"))
        ui.button("Refresh View", on_click=lambda: render_latest_report())

    with ui.row().classes("bq-toolbar w-full"):
        ui.button("Auto Update Data", on_click=lambda: _trigger("auto_update_data"))
        ui.button("Run System Analysis", on_click=lambda: _trigger("run_agent_system_analysis"))
        ui.button("Run Recommendation Cycle", on_click=lambda: _trigger("run_agent_cycle"))

    ui.separator()

    summary_container = ui.row().classes("bq-card-grid bq-metric-grid w-full")
    report_container = ui.card().classes("bq-card w-full")
    chat_container = ui.card().classes("bq-card w-full")

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
                with ui.card().classes("bq-card"):
                    ui.label(label).classes("text-sm text-slate-400")
                    ui.label(value).classes("text-lg font-semibold")

        with report_container:
            ui.label("Latest Agent Report").classes("text-xl font-semibold")
            ui.label(f"Created at: {latest['created_at']}").classes("text-xs text-slate-400")
            ui.markdown(str(latest["report_markdown"])).classes("w-full")

        with chat_container:
            ui.label("Ask BQuant").classes("text-xl font-semibold")
            ui.label("Live chat collects fresh BQuant context and calls the local LLM; deterministic fallback stays available.").classes(
                "text-sm text-slate-400"
            )
            chat_thread = ui.column().classes(
                "bq-chat-thread w-full gap-3 max-h-[560px] overflow-y-auto rounded border border-slate-700 bg-slate-950/40 p-4"
            )
            status_label = ui.label("").classes("text-xs text-slate-400")
            chat_history_empty = {"value": False}

            def render_message(
                *,
                role: str,
                content: str,
                metadata: str | None = None,
                pending: bool = False,
            ) -> None:
                """Render one chat bubble in the conversation thread."""
                is_user = role == "user"
                row_classes = "bq-chat-row justify-end" if is_user else "bq-chat-row justify-start"
                bubble_classes = (
                    "bq-chat-bubble rounded-lg px-4 py-3 shadow "
                    + (
                        "bg-blue-600 text-white"
                        if is_user
                        else "bg-slate-800 text-slate-100 border border-slate-700"
                    )
                )
                with chat_thread:
                    with ui.row().classes(row_classes):
                        with ui.column().classes("gap-1"):
                            with ui.card().classes(bubble_classes):
                                if pending:
                                    ui.spinner(size="sm").classes("mr-2")
                                    ui.label(content).classes("text-sm")
                                elif is_user:
                                    ui.label(content).classes("whitespace-pre-wrap text-sm")
                                else:
                                    ui.markdown(content).classes("bq-markdown w-full text-sm")
                            if metadata:
                                align_class = "text-right" if is_user else "text-left"
                                ui.label(metadata).classes(f"text-[11px] text-slate-500 {align_class}")

            def render_chat_history() -> None:
                """Load persisted chat history into the thread."""
                chat_thread.clear()
                try:
                    rows = load_recent_agent_chat_messages(limit=12)
                except Exception as exc:
                    LOGGER.log_error(
                        "load_recent_agent_chat_messages",
                        type(exc).__name__,
                        str(exc),
                        context={},
                        channel="web",
                    )
                    rows = []
                if not rows:
                    chat_history_empty["value"] = True
                    with chat_thread:
                        ui.label("No chat history yet. Ask a question to start.").classes("text-sm text-slate-500")
                    return
                chat_history_empty["value"] = False
                for row in rows:
                    render_message(
                        role="user",
                        content=str(row.get("question") or ""),
                        metadata=str(row.get("event_ts") or ""),
                    )
                    latency = row.get("latency_seconds")
                    latency_text = f", {float(latency):.1f}s" if latency is not None else ""
                    metadata = f"{row.get('answer_source')} / {row.get('model') or 'deterministic'}{latency_text}"
                    if row.get("error_message"):
                        metadata = f"{metadata} / fallback: {row.get('error_message')}"
                    render_message(
                        role="assistant",
                        content=str(row.get("answer_markdown") or ""),
                        metadata=metadata,
                    )

            question = ui.textarea(
                label="Message",
                placeholder="Ask about data freshness, VNIndex/VN30 trend, or portfolio allocation.",
            ).classes("w-full")

            def answer_question() -> None:
                """Answer the user question with live context and local LLM."""
                user_question = str(question.value or "").strip()
                if not user_question:
                    ui.notify("Enter a question first", type="warning", position="top")
                    return
                try:
                    if chat_history_empty["value"]:
                        chat_thread.clear()
                    render_message(role="user", content=user_question, metadata="Now")
                    render_message(role="assistant", content="Thinking with fresh BQuant context...", pending=True)
                    status_label.set_text("Collecting fresh BQuant context and calling the local LLM...")
                    status_label.update()
                    result = answer_live_system_question(user_question)
                    render_chat_history()
                    question.value = ""
                    question.update()
                    model = result.get("model") or "deterministic"
                    latency = result.get("latency_seconds")
                    latency_text = f", {latency:.1f}s" if latency is not None else ""
                    status_label.set_text(f"Answer source: {result['answer_source']} ({model}{latency_text})")
                    status_label.update()
                    LOGGER.log_web_event(
                        "Answered BQuant agent question",
                        event_type="agent_chat",
                        status="success",
                        question=user_question,
                        answer_source=result["answer_source"],
                        model=model,
                        latency_seconds=latency,
                    )
                except Exception as exc:
                    status_label.set_text("Agent chat failed.")
                    status_label.update()
                    LOGGER.log_error(
                        "answer_agent_question",
                        type(exc).__name__,
                        str(exc),
                        context={"question": user_question},
                        channel="web",
                    )
                    ui.notify(f"Unable to answer question: {exc}", type="negative", position="top")

            with ui.row().classes("bq-control-row w-full justify-end"):
                ui.button("Reload Chat", on_click=render_chat_history)
                ui.button("Send", on_click=answer_question)

            render_chat_history()

    render_latest_report()
