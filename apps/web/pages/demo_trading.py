"""Demo Trading page for agent-driven paper portfolio management."""

from __future__ import annotations

import html

from nicegui import ui
from nicegui.client import Client

from apps.web.services.demo_trading import (
    estimate_manual_trade,
    execute_all_plan_items,
    execute_plan_item,
    generate_daily_plan,
    load_demo_trading_snapshot,
    place_manual_trade,
    reset_demo_account,
)
from utils.logger import BQuantLogger


LOGGER = BQuantLogger("web_demo_trading_page", component="web", subcomponent="demo_trading_page", default_channel="web")


def render_demo_trading(client: Client) -> None:
    """Render the Demo Trading workspace.

    Args:
        client: NiceGUI client object supplied by the router. The page stores
            state in widgets and does not use the client directly.

    Side Effects:
        Creates portfolio controls, reads/writes demo trading ledger tables,
        and lets the current agent plan place filled orders in the demo account.
    """
    del client
    ui.label("Demo Trading").classes("bq-page-title font-bold")
    ui.label(
        "Agent-driven paper trading account with 50,000,000 VND capital, manual BUY/SELL tickets, and T+2.5 settlement."
    ).classes("bq-page-subtitle text-sm")
    ui.separator()

    with ui.row().classes("bq-toolbar w-full"):
        ui.button("Back to Dashboard", on_click=lambda: ui.navigate.to("/"))
        ui.button("Agents", on_click=lambda: ui.navigate.to("/agents"))
        ui.button("Operations", on_click=lambda: ui.navigate.to("/operations"))
        ui.button("Refresh View", on_click=lambda: render_snapshot())

    with ui.row().classes("bq-toolbar w-full"):
        ui.button("Generate Daily Plan", on_click=lambda: _generate_plan(refresh=False))
        ui.button("Refresh Agents + Plan", on_click=lambda: _generate_plan(refresh=True))
        execute_selected_button = ui.button("Agent Execute Selected", on_click=lambda: _execute_selected())
        ui.button("Agent Execute All BUY/SELL", on_click=lambda: _execute_all())

    ui.label(
        "Orders here are real ledger updates inside the demo account only. BUY lots are locked until T+2.5; SELL uses FIFO sellable lots."
    ).classes("text-xs text-slate-400")
    ui.separator()

    summary_container = ui.row().classes("bq-card-grid bq-metric-grid w-full")
    plan_meta_container = ui.card().classes("bq-card w-full")

    with ui.card().classes("bq-card w-full"):
        ui.label("Manual Trade Ticket").classes("text-xl font-semibold")
        ui.label(
            "Place explicit BUY/SELL orders with symbol, quantity, and execution price. SELL updates lots and cash; full SELL removes the symbol from current positions."
        ).classes("text-sm text-slate-400")
        with ui.row().classes("bq-control-row w-full"):
            trade_action_select = ui.select(options=["BUY", "SELL"], value="BUY", label="Action").classes(
                "bq-control-field"
            )
            trade_symbol_input = ui.input(label="Symbol", placeholder="STB").classes("bq-control-field")
            trade_quantity_input = ui.number(label="Quantity", value=100, format="%.0f").classes("bq-control-field")
            trade_price_input = ui.number(label="Price VND", value=0, format="%.0f").classes("bq-control-field")
            ui.button("Preview Trade", on_click=lambda: _open_trade_dialog_from_ticket())
            ui.button("Reset Demo Account", on_click=lambda: _reset_account())

    with ui.dialog() as trade_dialog, ui.card().classes("bq-card w-full max-w-3xl"):
        ui.label("Confirm Demo Trade").classes("text-xl font-semibold")
        ui.label("Review ticker price, quantity, fees/taxes, cash impact, and estimated average cost before execution.").classes(
            "text-sm text-slate-400"
        )
        with ui.row().classes("bq-control-row w-full"):
            dialog_action_select = ui.select(options=["BUY", "SELL"], value="BUY", label="Action").classes(
                "bq-control-field"
            )
            dialog_symbol_input = ui.input(label="Symbol").classes("bq-control-field")
            dialog_quantity_input = ui.number(label="Quantity", value=100, format="%.0f").classes(
                "bq-control-field"
            )
            dialog_price_input = ui.number(label="Execution price VND", value=0, format="%.0f").classes(
                "bq-control-field"
            )
        with ui.row().classes("bq-card-grid bq-metric-grid w-full"):
            preview_price = ui.label("Price: N/A").classes("bq-card text-sm")
            preview_gross = ui.label("Gross: N/A").classes("bq-card text-sm")
            preview_costs = ui.label("Fees/Tax: N/A").classes("bq-card text-sm")
            preview_net = ui.label("Net: N/A").classes("bq-card text-sm")
            preview_cash = ui.label("Cash after: N/A").classes("bq-card text-sm")
            preview_avg_cost = ui.label("Avg cost after: N/A").classes("bq-card text-sm")
            preview_realized = ui.label("Est. realized PnL: N/A").classes("bq-card text-sm")
            preview_sellable = ui.label("Sellable: N/A").classes("bq-card text-sm")
            preview_validation = ui.label("Validation: N/A").classes("bq-card text-sm")
        with ui.row().classes("bq-control-row w-full justify-end"):
            ui.button("Cancel", on_click=trade_dialog.close)
            ui.button("Execute Trade", on_click=lambda: _execute_dialog_trade())

    dialog_action_select.on("update:model-value", lambda _: _update_trade_preview())
    dialog_symbol_input.on("update:model-value", lambda _: _update_trade_preview())
    dialog_quantity_input.on("update:model-value", lambda _: _update_trade_preview())
    dialog_price_input.on("update:model-value", lambda _: _update_trade_preview())

    with ui.card().classes("bq-table-card w-full"):
        ui.label("Portfolio PnL + Agent Analysis").classes("text-xl font-semibold")
        ui.label(
            "Historical PnL is calculated from entry open to current close. This does not mutate the demo ledger cost basis."
        ).classes("text-sm text-slate-400")
        with ui.row().classes("bq-control-row w-full"):
            analysis_entry_input = ui.input(label="Buy point", value="2026-06-15").classes("bq-control-field")
            analysis_as_of_input = ui.input(label="Data as-of", value="2026-06-30").classes("bq-control-field")
            ui.button("Refresh Analysis", on_click=lambda: render_snapshot())
        analysis_container = ui.column().classes("w-full")

    with ui.card().classes("bq-table-card w-full"):
        ui.label("Daily Agent Plan").classes("text-xl font-semibold")
        selected_symbol = ui.select(options=[], label="Selected executable plan item").classes("w-full")
        plan_table = ui.table(
            columns=[
                {"name": "symbol", "label": "Symbol", "field": "symbol", "sortable": True},
                {"name": "proposed_action", "label": "Action", "field": "proposed_action", "sortable": True},
                {"name": "recommendation", "label": "Agent State", "field": "recommendation"},
                {"name": "suggested_quantity", "label": "Qty", "field": "suggested_quantity"},
                {"name": "latest_price", "label": "Price", "field": "latest_price"},
                {"name": "current_quantity", "label": "Holding", "field": "current_quantity"},
                {"name": "sellable_quantity", "label": "Sellable", "field": "sellable_quantity"},
                {"name": "suggested_weight", "label": "Target W.", "field": "suggested_weight"},
                {"name": "score", "label": "Score", "field": "score"},
                {"name": "risk_level", "label": "Risk", "field": "risk_level"},
            ],
            rows=[],
            pagination={"rowsPerPage": 15},
        ).classes("bq-data-table")

    with ui.card().classes("bq-table-card w-full"):
        ui.label("Current Positions").classes("text-xl font-semibold")
        positions_table = ui.table(
            columns=[
                {"name": "symbol", "label": "Symbol", "field": "symbol", "sortable": True},
                {"name": "t0_quantity", "label": "T+0", "field": "t0_quantity"},
                {"name": "t1_quantity", "label": "T+1", "field": "t1_quantity"},
                {"name": "t2_quantity", "label": "T+2", "field": "t2_quantity"},
                {"name": "sellable_quantity", "label": "Sellable", "field": "sellable_quantity"},
                {"name": "avg_cost", "label": "Avg Cost", "field": "avg_cost"},
                {"name": "latest_price", "label": "Price", "field": "latest_price"},
                {"name": "market_value", "label": "Market Value", "field": "market_value"},
                {"name": "unrealized_pnl", "label": "Unrealized PnL", "field": "unrealized_pnl"},
                {"name": "unrealized_pct", "label": "Unrealized %", "field": "unrealized_pct"},
            ],
            rows=[],
            pagination={"rowsPerPage": 12},
        ).classes("bq-data-table")

    with ui.card().classes("bq-table-card w-full"):
        ui.label("Trade History").classes("text-xl font-semibold")
        ui.label("Chronological ledger of IMPORT, BUY, and SELL events for the demo account.").classes(
            "text-sm text-slate-400"
        )
        trade_history_table = ui.table(
            columns=[
                {"name": "trade_time", "label": "Time", "field": "trade_time"},
                {"name": "action", "label": "Action", "field": "action"},
                {"name": "symbol", "label": "Symbol", "field": "symbol"},
                {"name": "quantity", "label": "Qty", "field": "quantity"},
                {"name": "price", "label": "Price", "field": "price"},
                {"name": "gross_amount", "label": "Gross", "field": "gross_amount"},
                {"name": "fees", "label": "Fees", "field": "fees"},
                {"name": "taxes", "label": "Taxes", "field": "taxes"},
                {"name": "net_amount", "label": "Net", "field": "net_amount"},
                {"name": "realized_pnl", "label": "Realized PnL", "field": "realized_pnl"},
                {"name": "status", "label": "Status", "field": "status"},
                {"name": "settlement_ts", "label": "Settlement", "field": "settlement_ts"},
                {"name": "source", "label": "Source", "field": "source"},
            ],
            rows=[],
            pagination={"rowsPerPage": 20},
        ).classes("bq-data-table")

    def _fmt_money(value: object) -> str:
        """Format VND-like amounts for table/card display."""
        try:
            return f"{float(value):,.0f}"
        except (TypeError, ValueError):
            return "N/A"

    def _fmt_pct(value: object) -> str:
        """Format decimal percentages for table/card display."""
        try:
            return f"{float(value) * 100:+.2f}%"
        except (TypeError, ValueError):
            return "N/A"

    def _generate_plan(*, refresh: bool) -> None:
        """Generate or regenerate today's daily agent plan."""
        try:
            result = generate_daily_plan(force=True, refresh_recommendations=refresh)
            ui.notify(
                f"Plan {result['status']} with {result['item_count']} items",
                type="positive",
                position="top",
            )
            render_snapshot()
        except Exception as exc:
            LOGGER.log_error(
                "generate_demo_trading_plan",
                type(exc).__name__,
                str(exc),
                context={"refresh_recommendations": refresh},
                channel="web",
            )
            ui.notify(f"Unable to generate plan: {exc}", type="negative", position="top")

    def _execute_selected() -> None:
        """Execute the selected BUY/SELL plan item."""
        if not selected_symbol.value:
            ui.notify("Select an executable BUY/SELL item first", type="warning", position="top")
            return
        try:
            result = execute_plan_item(str(selected_symbol.value))
            ui.notify(
                f"{result['action']} {result['quantity']:,} {result['symbol']} filled",
                type="positive",
                position="top",
            )
            render_snapshot()
        except Exception as exc:
            LOGGER.log_error(
                "execute_selected_demo_order",
                type(exc).__name__,
                str(exc),
                context={"selected_symbol": selected_symbol.value},
                channel="web",
            )
            ui.notify(f"Order failed: {exc}", type="negative", position="top")

    def _execute_all() -> None:
        """Execute all actionable BUY/SELL plan items."""
        try:
            result = execute_all_plan_items()
            ui.notify(
                f"Attempted {result['attempted']} orders; filled {result['filled']}",
                type="positive" if result["filled"] else "warning",
                position="top",
            )
            render_snapshot()
        except Exception as exc:
            LOGGER.log_error("execute_all_demo_orders", type(exc).__name__, str(exc), context={}, channel="web")
            ui.notify(f"Bulk execution failed: {exc}", type="negative", position="top")

    def _set_label_text(label, text: str, classes: str = "") -> None:
        """Update one NiceGUI label with optional extra trend classes."""
        label.text = text
        label.classes(remove="bq-positive bq-negative bq-warning bq-neutral")
        if classes:
            label.classes(add=classes)
        label.update()

    def _trade_preview_payload() -> dict[str, object]:
        """Return the current dialog trade estimate."""
        return estimate_manual_trade(
            action=str(dialog_action_select.value or ""),
            symbol=str(dialog_symbol_input.value or ""),
            quantity=int(float(dialog_quantity_input.value or 0)),
            price=float(dialog_price_input.value or 0),
        )

    def _update_trade_preview() -> None:
        """Refresh amount, cost, cash, and average-cost preview in the dialog."""
        try:
            preview = _trade_preview_payload()
        except Exception as exc:
            _set_label_text(preview_validation, f"Validation: {exc}", "bq-negative")
            return
        if float(dialog_price_input.value or 0) <= 0 and float(preview["price"]) > 0:
            dialog_price_input.value = float(preview["price"])
            dialog_price_input.update()
        costs_text = f"Fees/Tax: {_fmt_money(float(preview['fees']) + float(preview['taxes']))}"
        price_date = f" ({preview['latest_price_date']})" if preview.get("latest_price_date") else ""
        _set_label_text(preview_price, f"Price: {_fmt_money(preview['price'])}{price_date}")
        _set_label_text(preview_gross, f"Gross: {_fmt_money(preview['gross_amount'])}")
        _set_label_text(preview_costs, costs_text)
        _set_label_text(preview_net, f"Net: {_fmt_money(preview['net_amount'])}")
        _set_label_text(
            preview_cash,
            f"Cash after: {_fmt_money(preview['cash_after'])}",
            "bq-negative" if float(preview["cash_after"]) < 0 else "",
        )
        _set_label_text(preview_avg_cost, f"Avg cost after: {_fmt_money(preview['estimated_avg_cost'])}")
        _set_label_text(
            preview_realized,
            f"Est. realized PnL: {_fmt_money(preview['estimated_realized_pnl'])}",
            _value_color_class(preview["estimated_realized_pnl"]),
        )
        _set_label_text(preview_sellable, f"Sellable: {int(preview['sellable_quantity']):,}")
        if preview["is_valid"]:
            _set_label_text(preview_validation, "Validation: OK", "bq-positive")
        else:
            _set_label_text(preview_validation, f"Validation: {preview['validation_message']}", "bq-negative")

    def _open_trade_dialog(action: str, symbol: str, quantity: int, price: float) -> None:
        """Open the trade dialog with prefilled side, symbol, quantity, and price."""
        dialog_action_select.value = action.upper()
        dialog_symbol_input.value = symbol.upper()
        dialog_quantity_input.value = int(quantity)
        dialog_price_input.value = float(price)
        dialog_action_select.update()
        dialog_symbol_input.update()
        dialog_quantity_input.update()
        dialog_price_input.update()
        _update_trade_preview()
        trade_dialog.open()

    def _open_trade_dialog_from_ticket() -> None:
        """Open the trade dialog using the manual ticket fields."""
        _open_trade_dialog(
            action=str(trade_action_select.value or ""),
            symbol=str(trade_symbol_input.value or ""),
            quantity=int(float(trade_quantity_input.value or 0)),
            price=float(trade_price_input.value or 0),
        )

    def _execute_dialog_trade() -> None:
        """Execute the dialog trade after validating the preview."""
        try:
            preview = _trade_preview_payload()
            if not preview["is_valid"]:
                ui.notify(str(preview["validation_message"]), type="negative", position="top")
                _update_trade_preview()
                return
            result = place_manual_trade(
                action=str(dialog_action_select.value or ""),
                symbol=str(dialog_symbol_input.value or ""),
                quantity=int(float(dialog_quantity_input.value or 0)),
                price=float(dialog_price_input.value or 0),
            )
            ui.notify(
                f"{result['action']} {result['quantity']:,} {result['symbol']} filled at {_fmt_money(result['price'])} VND",
                type="positive",
                position="top",
            )
            trade_dialog.close()
            render_snapshot()
        except Exception as exc:
            LOGGER.log_error(
                "dialog_demo_trade",
                type(exc).__name__,
                str(exc),
                context={
                    "action": dialog_action_select.value,
                    "symbol": dialog_symbol_input.value,
                    "quantity": dialog_quantity_input.value,
                    "price": dialog_price_input.value,
                },
                channel="web",
            )
            ui.notify(f"Trade failed: {exc}", type="negative", position="top")
            _update_trade_preview()

    def _prefill_trade_ticket(action: str, row: dict[str, object]) -> None:
        """Open a trade dialog from one PnL table row."""
        action_text = action.upper()
        quantity = int(row.get("sellable_quantity") or 0) if action_text == "SELL" else 100
        trade_action_select.value = action_text
        trade_symbol_input.value = str(row.get("symbol") or "")
        trade_quantity_input.value = quantity
        trade_price_input.value = float(row.get("current_price") or 0)
        trade_action_select.update()
        trade_symbol_input.update()
        trade_quantity_input.update()
        trade_price_input.update()
        if action_text == "SELL" and quantity <= 0:
            ui.notify(
                f"{trade_symbol_input.value} has no sellable shares yet under T+2.5",
                type="warning",
                position="top",
            )
        else:
            _open_trade_dialog(
                action=action_text,
                symbol=str(row.get("symbol") or ""),
                quantity=quantity,
                price=float(row.get("current_price") or 0),
            )

    def _reset_account() -> None:
        """Reset the demo trading ledger."""
        try:
            result = reset_demo_account()
            ui.notify(f"Demo reset; cash {_fmt_money(result['cash_balance'])} VND", type="positive", position="top")
            render_snapshot()
        except Exception as exc:
            LOGGER.log_error("reset_demo_account", type(exc).__name__, str(exc), context={}, channel="web")
            ui.notify(f"Reset failed: {exc}", type="negative", position="top")

    def _value_color_class(value: object) -> str:
        """Return a text-color class for positive/negative/neutral values."""
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            return "bq-neutral"
        if numeric > 0.005:
            return "bq-positive"
        if numeric < -0.005:
            return "bq-negative"
        return "bq-neutral"

    def _action_color_class(action: object) -> str:
        """Return a text-color class for portfolio action buckets."""
        action_text = str(action)
        if action_text == "HOLD":
            return "bq-positive"
        if action_text == "SELL":
            return "bq-negative"
        if action_text in {"TRIM", "WATCH"}:
            return "bq-warning"
        return "bq-neutral"

    def _render_pnl_trade_table(rows: list[dict[str, object]]) -> None:
        """Render the interactive PnL table with per-row BUY/SELL controls."""
        if not rows:
            ui.label("No open positions to analyze.").classes("text-sm text-slate-400")
            return
        ui.html(
            """
            <style>
              .bq-pnl-table { width:100%; overflow-x:auto; border:1px solid rgba(148,163,184,0.18); border-radius:8px; margin-top:10px; }
              .bq-pnl-row {
                display:grid;
                grid-template-columns: 78px 64px 64px 64px 86px 90px 96px 112px 105px 118px 86px 94px 70px 86px 96px 132px;
                min-width:1460px;
                align-items:center;
                border-bottom:1px solid rgba(148,163,184,0.12);
              }
              .bq-pnl-row:last-child { border-bottom:none; }
              .bq-pnl-head { background:rgba(15,23,42,0.55); color:#93c5fd; font-size:11px; font-weight:700; }
              .bq-pnl-cell { padding:8px 10px; text-align:right; color:#e5e7eb; font-size:13px; }
              .bq-pnl-cell:first-child { text-align:left; }
              .bq-pnl-actions { display:flex; gap:6px; justify-content:flex-end; padding:6px 8px; }
              .bq-positive { color:#22c55e !important; }
              .bq-negative { color:#ef4444 !important; }
              .bq-warning { color:#f59e0b !important; }
              .bq-neutral { color:#cbd5e1 !important; }
            </style>
            """,
            sanitize=False,
        )
        headers = [
            "Symbol",
            "T+0",
            "T+1",
            "T+2",
            "Sellable",
            "Entry",
            "Current",
            "Gross PnL",
            "Gross Return",
            "Net If Closed",
            "Action",
            "Agent State",
            "Score",
            "Risk",
            "Trend",
            "Trade",
        ]
        with ui.element("div").classes("bq-pnl-table"):
            with ui.element("div").classes("bq-pnl-row bq-pnl-head"):
                for header in headers:
                    ui.label(header).classes("bq-pnl-cell")
            for row in rows:
                with ui.element("div").classes("bq-pnl-row"):
                    ui.label(str(row.get("symbol") or "")).classes("bq-pnl-cell font-semibold")
                    ui.label(f"{int(row.get('t0_quantity') or 0):,}").classes("bq-pnl-cell")
                    ui.label(f"{int(row.get('t1_quantity') or 0):,}").classes("bq-pnl-cell")
                    ui.label(f"{int(row.get('t2_quantity') or 0):,}").classes("bq-pnl-cell")
                    ui.label(f"{int(row.get('sellable_quantity') or 0):,}").classes("bq-pnl-cell")
                    ui.label(_fmt_money(row.get("entry_price"))).classes("bq-pnl-cell")
                    ui.label(_fmt_money(row.get("current_price"))).classes("bq-pnl-cell")
                    ui.label(_fmt_money(row.get("gross_pnl"))).classes(
                        f"bq-pnl-cell {_value_color_class(row.get('gross_pnl'))}"
                    )
                    ui.label(_fmt_pct(row.get("gross_return"))).classes(
                        f"bq-pnl-cell {_value_color_class(row.get('gross_return'))}"
                    )
                    ui.label(_fmt_money(row.get("net_if_closed_pnl"))).classes(
                        f"bq-pnl-cell {_value_color_class(row.get('net_if_closed_pnl'))}"
                    )
                    ui.label(str(row.get("action") or "")).classes(
                        f"bq-pnl-cell font-semibold {_action_color_class(row.get('action'))}"
                    )
                    ui.label(str(row.get("recommendation") or "")).classes("bq-pnl-cell")
                    ui.label(f"{float(row.get('score') or 0):.3f}").classes("bq-pnl-cell")
                    ui.label(str(row.get("risk_level") or "")).classes("bq-pnl-cell")
                    ui.label(str(row.get("trend_state") or "")).classes("bq-pnl-cell")
                    with ui.element("div").classes("bq-pnl-actions"):
                        ui.button("BUY", on_click=lambda row=row: _prefill_trade_ticket("BUY", row)).props(
                            "dense unelevated color=positive size=sm"
                        )
                        ui.button("SELL", on_click=lambda row=row: _prefill_trade_ticket("SELL", row)).props(
                            "dense unelevated color=negative size=sm"
                        )

    def render_snapshot() -> None:
        """Reload account, plan, position, and order tables."""
        summary_container.clear()
        plan_meta_container.clear()
        analysis_container.clear()
        try:
            snapshot = load_demo_trading_snapshot(
                analysis_entry_date=analysis_entry_input.value,
                analysis_as_of_date=analysis_as_of_input.value,
            )
        except Exception as exc:
            LOGGER.log_error("load_demo_trading_snapshot", type(exc).__name__, str(exc), context={}, channel="web")
            ui.notify(f"Unable to load demo trading snapshot: {exc}", type="negative", position="top")
            return

        portfolio = snapshot["portfolio"]
        account = snapshot["account"]
        plan = snapshot.get("plan")
        with summary_container:
            cards = [
                ("Initial Capital", _fmt_money(account["initial_cash"])),
                ("Cash", _fmt_money(portfolio["cash_balance"])),
                ("Market Value", _fmt_money(portfolio["market_value"])),
                ("Equity", _fmt_money(portfolio["equity"])),
                ("Unrealized PnL", _fmt_money(portfolio["unrealized_pnl"])),
                ("Unrealized %", _fmt_pct(portfolio["unrealized_pct"])),
            ]
            for label, value in cards:
                with ui.card().classes("bq-card"):
                    ui.label(label).classes("text-sm text-slate-400")
                    ui.label(value).classes("text-lg font-semibold")

        with plan_meta_container:
            ui.label("Plan Metadata").classes("text-xl font-semibold")
            if plan:
                summary = plan.get("summary", {})
                ui.label(f"Plan date: {plan['plan_date']}")
                ui.label(f"Source as-of date: {plan.get('source_as_of_date')}")
                ui.label(f"Expected previous trading day: {summary.get('expected_previous_trading_day', 'N/A')}")
                ui.label(f"Agent run: {plan.get('source_run_id')}")
                ui.label(f"Action counts: {summary.get('action_counts', {})}")
            else:
                ui.label("No daily plan yet. Generate Daily Plan to create one from the latest agent recommendations.").classes(
                    "text-sm text-slate-400"
                )

        with analysis_container:
            ui.html(_render_portfolio_analysis_summary_html(snapshot["portfolio_analysis"]), sanitize=False).classes(
                "w-full"
            )
            _render_pnl_trade_table(snapshot["portfolio_analysis"].get("rows") or [])
            ui.html(_render_portfolio_reason_cards_html(snapshot["portfolio_analysis"]), sanitize=False).classes(
                "w-full"
            )

        plan_rows = [_format_plan_row(row, _fmt_money, _fmt_pct) for row in snapshot["plan_items"]]
        positions_rows = [_format_position_row(row, _fmt_money, _fmt_pct) for row in snapshot["positions"]]
        trade_history_rows = [_format_order_row(row, _fmt_money) for row in snapshot["trade_history"]]

        executable_symbols = [
            row["symbol"]
            for row in snapshot["plan_items"]
            if row["proposed_action"] in {"BUY", "SELL"} and int(row["suggested_quantity"]) > 0
        ]
        selected_symbol.options = executable_symbols
        if selected_symbol.value not in executable_symbols:
            selected_symbol.value = executable_symbols[0] if executable_symbols else None
        selected_symbol.update()
        execute_selected_button.disable() if not executable_symbols else execute_selected_button.enable()

        plan_table.rows = plan_rows
        positions_table.rows = positions_rows
        trade_history_table.rows = trade_history_rows
        plan_table.update()
        positions_table.update()
        trade_history_table.update()

    render_snapshot()


def _format_plan_row(row: dict[str, object], money_formatter, pct_formatter) -> dict[str, object]:
    """Format one plan row for NiceGUI table display."""
    return {
        "symbol": row["symbol"],
        "proposed_action": row["proposed_action"],
        "recommendation": row["recommendation"],
        "suggested_quantity": f"{int(row['suggested_quantity']):,}",
        "latest_price": money_formatter(row["latest_price"]),
        "current_quantity": f"{int(row['current_quantity']):,}",
        "sellable_quantity": f"{int(row['sellable_quantity']):,}",
        "suggested_weight": pct_formatter(row["suggested_weight"]),
        "score": f"{float(row['score']):.3f}",
        "risk_level": row["risk_level"],
    }


def _format_position_row(row: dict[str, object], money_formatter, pct_formatter) -> dict[str, object]:
    """Format one current position row for NiceGUI table display."""
    return {
        "symbol": row["symbol"],
        "t0_quantity": f"{int(row.get('t0_quantity', 0)):,}",
        "t1_quantity": f"{int(row.get('t1_quantity', 0)):,}",
        "t2_quantity": f"{int(row.get('t2_quantity', 0)):,}",
        "sellable_quantity": f"{int(row['sellable_quantity']):,}",
        "avg_cost": money_formatter(row["avg_cost"]),
        "latest_price": money_formatter(row["latest_price"]),
        "market_value": money_formatter(row["market_value"]),
        "unrealized_pnl": money_formatter(row["unrealized_pnl"]),
        "unrealized_pct": pct_formatter(row["unrealized_pct"]),
    }


def _format_order_row(row: dict[str, object], money_formatter) -> dict[str, object]:
    """Format one trade-history row for NiceGUI table display."""
    return {
        "trade_time": row["trade_time"],
        "action": row["action"],
        "symbol": row["symbol"],
        "quantity": f"{int(row['quantity']):,}",
        "price": money_formatter(row["price"]),
        "gross_amount": money_formatter(row["gross_amount"]),
        "fees": money_formatter(row["fees"]),
        "taxes": money_formatter(row["taxes"]),
        "net_amount": money_formatter(row["net_amount"]),
        "realized_pnl": money_formatter(row["realized_pnl"]),
        "status": row["status"],
        "settlement_ts": row["settlement_ts"],
        "source": row["source"],
    }


def _analysis_money(value: object) -> str:
    """Format an analysis amount as VND text."""
    try:
        return f"{float(value):,.0f}"
    except (TypeError, ValueError):
        return "N/A"


def _analysis_pct(value: object) -> str:
    """Format an analysis decimal as signed percentage text."""
    try:
        return f"{float(value) * 100:+.2f}%"
    except (TypeError, ValueError):
        return "N/A"


def _analysis_value_class(value: object) -> str:
    """Return a color class for a positive, negative, or neutral value."""
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return "bq-neutral"
    if numeric > 0.005:
        return "bq-positive"
    if numeric < -0.005:
        return "bq-negative"
    return "bq-neutral"


def _analysis_action_class(action: object) -> str:
    """Return a color class for portfolio action text."""
    action_text = str(action)
    if action_text == "HOLD":
        return "bq-positive"
    if action_text == "SELL":
        return "bq-negative"
    if action_text in {"TRIM", "WATCH"}:
        return "bq-warning"
    return "bq-neutral"


def _render_portfolio_analysis_summary_html(analysis: dict[str, object]) -> str:
    """Render the top-level PnL summary with trend-colored values."""
    summary = dict(analysis.get("summary") or {})

    def esc(value: object) -> str:
        return html.escape(str(value if value is not None else ""))

    return f"""
    <div class="bq-analysis-root">
      <style>
        .bq-analysis-root {{ color:#e5e7eb; font-size:13px; }}
        .bq-analysis-summary {{
          display:grid; grid-template-columns:repeat(auto-fit, minmax(140px, 1fr));
          gap:10px; margin:12px 0 14px 0;
        }}
        .bq-analysis-summary div {{
          border:1px solid rgba(148,163,184,0.18); border-radius:8px;
          background:rgba(15,23,42,0.45); padding:10px 12px;
        }}
        .bq-analysis-summary span {{
          display:block; color:#93c5fd; font-size:11px; margin-bottom:4px;
        }}
        .bq-analysis-summary strong {{ font-size:14px; }}
        .bq-positive {{ color:#22c55e !important; }}
        .bq-negative {{ color:#ef4444 !important; }}
        .bq-warning {{ color:#f59e0b !important; }}
        .bq-neutral {{ color:#cbd5e1 !important; }}
      </style>
      <div class="bq-analysis-summary">
        <div><span>Entry</span><strong>{esc(summary.get("entry_date"))}</strong></div>
        <div><span>As-of</span><strong>{esc(summary.get("as_of_date"))}</strong></div>
        <div><span>Entry Value</span><strong>{_analysis_money(summary.get("entry_value"))}</strong></div>
        <div><span>Current Value</span><strong>{_analysis_money(summary.get("current_value"))}</strong></div>
        <div><span>Gross PnL</span><strong class="{_analysis_value_class(summary.get("gross_pnl"))}">{_analysis_money(summary.get("gross_pnl"))}</strong></div>
        <div><span>Gross Return</span><strong class="{_analysis_value_class(summary.get("gross_return"))}">{_analysis_pct(summary.get("gross_return"))}</strong></div>
        <div><span>Net If Closed</span><strong class="{_analysis_value_class(summary.get("net_if_closed_pnl"))}">{_analysis_money(summary.get("net_if_closed_pnl"))}</strong></div>
        <div><span>W/L/N</span><strong>{int(summary.get("winners", 0))}/{int(summary.get("losers", 0))}/{int(summary.get("neutral", 0))}</strong></div>
      </div>
    </div>
    """


def _render_portfolio_reason_cards_html(analysis: dict[str, object]) -> str:
    """Render per-symbol buy, hold, and sell reasoning cards."""
    rows = list(analysis.get("rows") or [])
    if not rows:
        return "<div class='bq-analysis-empty'>No open positions to analyze.</div>"

    def esc(value: object) -> str:
        return html.escape(str(value if value is not None else ""))

    reason_cards = []
    for row in rows:
        action_class = _analysis_action_class(row.get("action"))
        reason = dict(row.get("reasons") or {})
        reason_cards.append(
            f"""
            <div class="bq-reason-card">
              <div class="bq-reason-head">
                <strong>{esc(row.get('symbol'))}</strong>
                <span class="{action_class}">{esc(row.get('action'))}</span>
              </div>
              <p><span>Buy thesis</span>{esc(reason.get('buy'))}</p>
              <p><span>Hold case</span>{esc(reason.get('hold'))}</p>
              <p><span>Sell/trim trigger</span>{esc(reason.get('sell'))}</p>
            </div>
            """
        )
    return f"""
    <div class="bq-analysis-root">
      <style>
        .bq-positive {{ color:#22c55e !important; }}
        .bq-negative {{ color:#ef4444 !important; }}
        .bq-warning {{ color:#f59e0b !important; }}
        .bq-neutral {{ color:#cbd5e1 !important; }}
        .bq-reason-grid {{
          display:grid; grid-template-columns:repeat(auto-fit, minmax(300px, 1fr));
          gap:12px; margin-top:14px;
        }}
        .bq-reason-card {{
          border:1px solid rgba(148,163,184,0.18); border-radius:8px;
          background:rgba(15,23,42,0.42); padding:12px;
        }}
        .bq-reason-head {{ display:flex; justify-content:space-between; gap:10px; margin-bottom:8px; }}
        .bq-reason-card p {{ margin:8px 0 0 0; line-height:1.45; color:#cbd5e1; }}
        .bq-reason-card p span {{
          display:block; color:#93c5fd; font-size:11px; font-weight:700; text-transform:uppercase; margin-bottom:2px;
        }}
        .bq-analysis-empty {{ color:#94a3b8; padding:10px 0; }}
      </style>
      <div class="bq-reason-grid">{''.join(reason_cards)}</div>
    </div>
    """


def _render_portfolio_analysis_html(analysis: dict[str, object]) -> str:
    """Render colored portfolio PnL and agent reasoning.

    Args:
        analysis: Payload returned by
            `apps.web.services.demo_trading.load_demo_portfolio_analysis`.

    Returns:
        Safe HTML string with local CSS classes. Text color encodes trend:
        positive values are green, negative values are red, neutral/watch
        values are amber or slate.
    """
    summary = dict(analysis.get("summary") or {})
    rows = list(analysis.get("rows") or [])
    if not rows:
        return "<div class='bq-analysis-empty'>No open positions to analyze.</div>"

    def money(value: object) -> str:
        try:
            return f"{float(value):,.0f}"
        except (TypeError, ValueError):
            return "N/A"

    def pct(value: object) -> str:
        try:
            return f"{float(value) * 100:+.2f}%"
        except (TypeError, ValueError):
            return "N/A"

    def css_for_value(value: object) -> str:
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            return "bq-neutral"
        if numeric > 0.005:
            return "bq-positive"
        if numeric < -0.005:
            return "bq-negative"
        return "bq-neutral"

    def css_for_action(action: object) -> str:
        action_text = str(action)
        if action_text == "HOLD":
            return "bq-positive"
        if action_text == "SELL":
            return "bq-negative"
        if action_text in {"TRIM", "WATCH"}:
            return "bq-warning"
        return "bq-neutral"

    def esc(value: object) -> str:
        return html.escape(str(value if value is not None else ""))

    summary_html = f"""
      <div class="bq-analysis-summary">
        <div><span>Entry</span><strong>{esc(summary.get("entry_date"))}</strong></div>
        <div><span>As-of</span><strong>{esc(summary.get("as_of_date"))}</strong></div>
        <div><span>Entry Value</span><strong>{money(summary.get("entry_value"))}</strong></div>
        <div><span>Current Value</span><strong>{money(summary.get("current_value"))}</strong></div>
        <div><span>Gross PnL</span><strong class="{css_for_value(summary.get("gross_pnl"))}">{money(summary.get("gross_pnl"))}</strong></div>
        <div><span>Gross Return</span><strong class="{css_for_value(summary.get("gross_return"))}">{pct(summary.get("gross_return"))}</strong></div>
        <div><span>Net If Closed</span><strong class="{css_for_value(summary.get("net_if_closed_pnl"))}">{money(summary.get("net_if_closed_pnl"))}</strong></div>
        <div><span>W/L/N</span><strong>{int(summary.get("winners", 0))}/{int(summary.get("losers", 0))}/{int(summary.get("neutral", 0))}</strong></div>
      </div>
    """

    table_rows = []
    reason_cards = []
    for row in rows:
        action_class = css_for_action(row.get("action"))
        pnl_class = css_for_value(row.get("gross_pnl"))
        return_class = css_for_value(row.get("gross_return"))
        table_rows.append(
            "<tr>"
            f"<td><strong>{esc(row.get('symbol'))}</strong></td>"
            f"<td>{int(row.get('quantity') or 0):,}</td>"
            f"<td>{money(row.get('entry_price'))}</td>"
            f"<td>{money(row.get('current_price'))}</td>"
            f"<td class='{pnl_class}'>{money(row.get('gross_pnl'))}</td>"
            f"<td class='{return_class}'>{pct(row.get('gross_return'))}</td>"
            f"<td class='{css_for_value(row.get('net_if_closed_pnl'))}'>{money(row.get('net_if_closed_pnl'))}</td>"
            f"<td class='{action_class}'><strong>{esc(row.get('action'))}</strong></td>"
            f"<td>{esc(row.get('recommendation'))}</td>"
            f"<td>{float(row.get('score') or 0):.3f}</td>"
            f"<td>{esc(row.get('risk_level'))}</td>"
            f"<td>{esc(row.get('trend_state'))}</td>"
            f"</tr>"
        )
        reason = dict(row.get("reasons") or {})
        reason_cards.append(
            f"""
            <div class="bq-reason-card">
              <div class="bq-reason-head">
                <strong>{esc(row.get('symbol'))}</strong>
                <span class="{action_class}">{esc(row.get('action'))}</span>
              </div>
              <p><span>Buy thesis</span>{esc(reason.get('buy'))}</p>
              <p><span>Hold case</span>{esc(reason.get('hold'))}</p>
              <p><span>Sell/trim trigger</span>{esc(reason.get('sell'))}</p>
            </div>
            """
        )

    return f"""
    <div class="bq-analysis-root">
      <style>
        .bq-analysis-root {{ color:#e5e7eb; font-size:13px; }}
        .bq-analysis-summary {{
          display:grid; grid-template-columns:repeat(auto-fit, minmax(140px, 1fr));
          gap:10px; margin:12px 0 14px 0;
        }}
        .bq-analysis-summary div {{
          border:1px solid rgba(148,163,184,0.18); border-radius:8px;
          background:rgba(15,23,42,0.45); padding:10px 12px;
        }}
        .bq-analysis-summary span {{
          display:block; color:#93c5fd; font-size:11px; margin-bottom:4px;
        }}
        .bq-analysis-summary strong {{ font-size:14px; }}
        .bq-analysis-table-wrap {{ overflow-x:auto; border:1px solid rgba(148,163,184,0.18); border-radius:8px; }}
        .bq-analysis-table {{ width:100%; border-collapse:collapse; min-width:1040px; }}
        .bq-analysis-table th, .bq-analysis-table td {{
          padding:8px 10px; border-bottom:1px solid rgba(148,163,184,0.12); text-align:right;
        }}
        .bq-analysis-table th {{ color:#93c5fd; font-size:11px; font-weight:700; }}
        .bq-analysis-table th:first-child, .bq-analysis-table td:first-child {{ text-align:left; }}
        .bq-positive {{ color:#22c55e !important; }}
        .bq-negative {{ color:#ef4444 !important; }}
        .bq-warning {{ color:#f59e0b !important; }}
        .bq-neutral {{ color:#cbd5e1 !important; }}
        .bq-reason-grid {{
          display:grid; grid-template-columns:repeat(auto-fit, minmax(300px, 1fr));
          gap:12px; margin-top:14px;
        }}
        .bq-reason-card {{
          border:1px solid rgba(148,163,184,0.18); border-radius:8px;
          background:rgba(15,23,42,0.42); padding:12px;
        }}
        .bq-reason-head {{ display:flex; justify-content:space-between; gap:10px; margin-bottom:8px; }}
        .bq-reason-card p {{ margin:8px 0 0 0; line-height:1.45; color:#cbd5e1; }}
        .bq-reason-card p span {{
          display:block; color:#93c5fd; font-size:11px; font-weight:700; text-transform:uppercase; margin-bottom:2px;
        }}
        .bq-analysis-empty {{ color:#94a3b8; padding:10px 0; }}
      </style>
      {summary_html}
      <div class="bq-analysis-table-wrap">
        <table class="bq-analysis-table">
          <thead>
            <tr>
              <th>Symbol</th><th>Qty</th><th>Entry</th><th>Current</th><th>Gross PnL</th>
              <th>Gross Return</th><th>Net If Closed</th><th>Action</th><th>Agent State</th>
              <th>Score</th><th>Risk</th><th>Trend</th>
            </tr>
          </thead>
          <tbody>{''.join(table_rows)}</tbody>
        </table>
      </div>
      <div class="bq-reason-grid">{''.join(reason_cards)}</div>
    </div>
    """
