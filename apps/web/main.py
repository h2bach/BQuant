"""BQuant Web Application - FastAPI + NiceGUI Dashboard."""

from __future__ import annotations

import sys
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from nicegui import app, ui
from nicegui.client import Client

from warehouse.init_observability import initialize_observability
from warehouse.refresh_state import ensure_refresh_state_table

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
APP_ROOT = Path(__file__).resolve().parent

# Add REPO_ROOT to sys.path for imports
sys.path.insert(0, str(REPO_ROOT))

# FastAPI app
fastapi_app = FastAPI(
    title="BQuant Platform",
    description="Quantitative Research Platform for Vietnam Stock Market",
    version="0.1.0",
)

# CORS middleware
fastapi_app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@ui.page("/")
def dashboard(client: Client):
    """Main dashboard page."""
    from apps.web.pages.dashboard import render_dashboard
    render_dashboard(client)


@ui.page("/catalog")
def catalog(client: Client):
    """Data catalog page."""
    from apps.web.pages.catalog import render_catalog
    render_catalog(client)


@ui.page("/symbol")
def symbol_explorer(client: Client):
    """Symbol explorer page."""
    from apps.web.pages.symbol_explorer import render_symbol_explorer
    render_symbol_explorer(client)


@ui.page("/manifest")
def manifest(client: Client):
    """Data manifest page."""
    from apps.web.pages.manifest import render_manifest
    render_manifest(client)


@ui.page("/quality")
def quality(client: Client):
    """Data quality page."""
    from apps.web.pages.quality import render_quality
    render_quality(client)


@ui.page("/sql_lab")
def sql_lab(client: Client):
    """SQL Lab page."""
    from apps.web.pages.sql_lab import render_sql_lab
    render_sql_lab(client)


@ui.page("/operations")
def operations(client: Client):
    """Operations monitoring page."""
    from apps.web.pages.operations import render_operations
    render_operations(client)


@ui.page("/alerts")
def alerts(client: Client):
    """Alerts page."""
    from apps.web.pages.alerts import render_alerts
    render_alerts(client)


def main():
    """Run the NiceGUI application."""
    initialize_observability()
    ensure_refresh_state_table()
    ui.run(
        title="BQuant Platform",
        port=6688,
        host="0.0.0.0",
        dark=True,
        binding_refresh_interval=0.5,
    )


if __name__ in {"__main__", "__mp_main__"}:
    main()
