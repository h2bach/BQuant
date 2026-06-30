"""Demo Trading page for agent-driven paper portfolio management."""

from __future__ import annotations

from nicegui import ui
from nicegui.client import Client

from apps.web.services.demo_trading import (
    execute_all_plan_items,
    execute_plan_item,
    generate_daily_plan,
    import_portfolio,
    load_demo_trading_snapshot,
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
        "Agent-driven paper trading account with 50,000,000 VND capital, user portfolio import, and T+2.5 settlement."
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
        ui.label("Import Existing Portfolio").classes("text-xl font-semibold")
        ui.label(
            "Paste CSV: symbol,quantity,avg_cost. Import replaces the current demo ledger and treats holdings as already settled."
        ).classes("text-sm text-slate-400")
        import_text = ui.textarea(
            value="symbol,quantity,avg_cost\nFPT,100,115000\nVCB,100,92000",
            placeholder="symbol,quantity,avg_cost",
        ).classes("w-full h-32 font-mono")
        with ui.row().classes("bq-control-row w-full"):
            cash_input = ui.number(label="Cash after import (optional)", value=None, format="%.0f").classes(
                "bq-control-field-wide"
            )
            ui.button("Import Portfolio", on_click=lambda: _import_portfolio())
            ui.button("Reset Demo Account", on_click=lambda: _reset_account())

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
                {"name": "quantity", "label": "Qty", "field": "quantity"},
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
        ui.label("Recent Demo Orders").classes("text-xl font-semibold")
        orders_table = ui.table(
            columns=[
                {"name": "trade_time", "label": "Time", "field": "trade_time"},
                {"name": "action", "label": "Action", "field": "action"},
                {"name": "symbol", "label": "Symbol", "field": "symbol"},
                {"name": "quantity", "label": "Qty", "field": "quantity"},
                {"name": "price", "label": "Price", "field": "price"},
                {"name": "net_amount", "label": "Net", "field": "net_amount"},
                {"name": "realized_pnl", "label": "Realized PnL", "field": "realized_pnl"},
                {"name": "settlement_ts", "label": "Settlement", "field": "settlement_ts"},
                {"name": "source", "label": "Source", "field": "source"},
            ],
            rows=[],
            pagination={"rowsPerPage": 12},
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

    def _import_portfolio() -> None:
        """Import user holdings as the demo starting portfolio."""
        try:
            cash_value = cash_input.value
            result = import_portfolio(
                str(import_text.value or ""),
                cash_balance=float(cash_value) if cash_value is not None else None,
            )
            ui.notify(
                f"Imported {result['imported_rows']} rows; cash {_fmt_money(result['cash_balance'])} VND",
                type="positive",
                position="top",
            )
            render_snapshot()
        except Exception as exc:
            LOGGER.log_error("import_demo_portfolio", type(exc).__name__, str(exc), context={}, channel="web")
            ui.notify(f"Import failed: {exc}", type="negative", position="top")

    def _reset_account() -> None:
        """Reset the demo trading ledger."""
        try:
            result = reset_demo_account()
            ui.notify(f"Demo reset; cash {_fmt_money(result['cash_balance'])} VND", type="positive", position="top")
            render_snapshot()
        except Exception as exc:
            LOGGER.log_error("reset_demo_account", type(exc).__name__, str(exc), context={}, channel="web")
            ui.notify(f"Reset failed: {exc}", type="negative", position="top")

    def render_snapshot() -> None:
        """Reload account, plan, position, and order tables."""
        summary_container.clear()
        plan_meta_container.clear()
        try:
            snapshot = load_demo_trading_snapshot()
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

        plan_rows = [_format_plan_row(row, _fmt_money, _fmt_pct) for row in snapshot["plan_items"]]
        positions_rows = [_format_position_row(row, _fmt_money, _fmt_pct) for row in snapshot["positions"]]
        order_rows = [_format_order_row(row, _fmt_money) for row in snapshot["orders"]]

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
        orders_table.rows = order_rows
        plan_table.update()
        positions_table.update()
        orders_table.update()

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
        "quantity": f"{int(row['quantity']):,}",
        "sellable_quantity": f"{int(row['sellable_quantity']):,}",
        "avg_cost": money_formatter(row["avg_cost"]),
        "latest_price": money_formatter(row["latest_price"]),
        "market_value": money_formatter(row["market_value"]),
        "unrealized_pnl": money_formatter(row["unrealized_pnl"]),
        "unrealized_pct": pct_formatter(row["unrealized_pct"]),
    }


def _format_order_row(row: dict[str, object], money_formatter) -> dict[str, object]:
    """Format one order row for NiceGUI table display."""
    return {
        "trade_time": row["trade_time"],
        "action": row["action"],
        "symbol": row["symbol"],
        "quantity": f"{int(row['quantity']):,}",
        "price": money_formatter(row["price"]),
        "net_amount": money_formatter(row["net_amount"]),
        "realized_pnl": money_formatter(row["realized_pnl"]),
        "settlement_ts": row["settlement_ts"],
        "source": row["source"],
    }
