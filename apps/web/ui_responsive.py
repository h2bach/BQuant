"""Shared responsive styles for the BQuant NiceGUI application."""

from __future__ import annotations

from nicegui import ui


_RESPONSIVE_STYLE_ID = "bq-responsive-styles"


def apply_responsive_styles() -> None:
    """Register global responsive CSS for all NiceGUI pages.

    Side Effects:
        Injects a single `<style>` block into the document head. The rules
        standardize page width, wrapping toolbars, card grids, scrollable
        tables, chart containers, and chat bubbles across the whole web app.
    """
    ui.add_head_html(
        f"""
<style id="{_RESPONSIVE_STYLE_ID}">
  html,
  body {{
    max-width: 100%;
    overflow-x: hidden;
  }}

  #q-app,
  .q-layout,
  .q-page-container,
  .q-page,
  .nicegui-content {{
    width: 100%;
    max-width: 100vw;
    min-width: 0;
    box-sizing: border-box;
    overflow-x: hidden;
  }}

  .nicegui-content {{
    padding: clamp(12px, 2vw, 24px);
    gap: 12px;
  }}

  .nicegui-content > * {{
    max-width: 100%;
    min-width: 0;
    box-sizing: border-box;
  }}

  .bq-page-title {{
    font-size: clamp(1.75rem, 3.2vw, 2.25rem);
    line-height: 1.1;
    overflow-wrap: anywhere;
  }}

  .bq-page-subtitle {{
    color: #94a3b8;
    overflow-wrap: anywhere;
  }}

  .bq-toolbar,
  .bq-control-row,
  .bq-header-row {{
    display: flex !important;
    flex-wrap: wrap;
    align-items: center;
    gap: 0.75rem;
    width: 100%;
    min-width: 0;
  }}

  .bq-header-row {{
    justify-content: space-between;
  }}

  .bq-control-row {{
    justify-content: flex-start;
  }}

  .bq-control-row.justify-end {{
    justify-content: flex-end;
  }}

  .bq-control-field {{
    width: 10rem;
    min-width: 9rem;
    max-width: 100%;
  }}

  .bq-control-field-wide {{
    width: 18rem;
    min-width: 12rem;
    max-width: 100%;
  }}

  .bq-toolbar .q-btn,
  .bq-control-row .q-btn {{
    min-height: 36px;
  }}

  .bq-card,
  .bq-fill-card,
  .q-card {{
    min-width: 0;
    max-width: 100%;
    box-sizing: border-box;
  }}

  .bq-card-grid {{
    display: grid !important;
    grid-template-columns: repeat(auto-fit, minmax(min(100%, 260px), 1fr));
    gap: 1rem;
    align-items: stretch;
    width: 100%;
    min-width: 0;
  }}

  .bq-metric-grid {{
    grid-template-columns: repeat(auto-fit, minmax(min(100%, 170px), 1fr));
    gap: 0.75rem;
  }}

  .bq-chart-card,
  .bq-table-card {{
    width: 100%;
    min-width: 0;
    overflow: hidden;
  }}

  .bq-chart-html {{
    width: 100%;
    min-width: 0;
    overflow: hidden;
  }}

  .bq-data-table {{
    width: 100%;
    max-width: 100%;
    min-width: 0;
  }}

  .bq-data-table.q-table__container,
  .bq-data-table .q-table__container,
  .bq-data-table .q-table__middle,
  .bq-table-card .q-table__container,
  .bq-table-card .q-table__middle {{
    width: 100%;
    max-width: 100%;
    overflow-x: auto;
  }}

  .bq-data-table .q-table {{
    min-width: 720px;
  }}

  .bq-lwc-root,
  .bq-lwc-pane {{
    max-width: 100%;
    min-width: 0;
  }}

  .bq-chat-thread {{
    width: 100%;
    max-width: 100%;
    min-width: 0;
  }}

  .bq-chat-row {{
    display: flex;
    width: 100%;
    min-width: 0;
  }}

  .bq-chat-bubble {{
    max-width: min(78%, 920px);
    min-width: 0;
    overflow-wrap: anywhere;
  }}

  .bq-markdown,
  .bq-markdown * {{
    max-width: 100%;
    overflow-wrap: anywhere;
  }}

  .bq-markdown pre,
  .bq-markdown code {{
    white-space: pre-wrap;
    overflow-wrap: anywhere;
  }}

  @media (max-width: 900px) {{
    .nicegui-content {{
      padding: 12px;
    }}

    .bq-header-row {{
      align-items: stretch;
    }}

    .bq-header-row > *,
    .bq-control-row,
    .bq-control-field,
    .bq-control-field-wide,
    .bq-control-row .q-btn,
    .bq-toolbar .q-btn {{
      width: 100%;
    }}

    .bq-card-grid {{
      grid-template-columns: 1fr;
      gap: 0.75rem;
    }}

    .bq-data-table .q-table {{
      min-width: 640px;
    }}

    .bq-lwc-root {{
      border-radius: 6px !important;
      min-height: auto !important;
    }}

    .bq-lwc-root .bq-lwc-price-pane {{
      height: clamp(300px, 55vh, 430px) !important;
    }}

    .bq-lwc-root .bq-lwc-signal-pane {{
      height: clamp(160px, 30vh, 230px) !important;
    }}

    .bq-lwc-root .bq-lwc-head {{
      flex-direction: column;
      align-items: flex-start !important;
    }}

    .bq-lwc-root .bq-lwc-status {{
      white-space: normal !important;
    }}

    .bq-lwc-root .bq-lwc-legend {{
      max-height: 128px;
      overflow-y: auto;
    }}

    .bq-lwc-root .bq-lwc-tooltip {{
      max-width: calc(100vw - 40px) !important;
      min-width: 0 !important;
    }}

    .bq-lwc-root .bq-lwc-tooltip-grid {{
      grid-template-columns: repeat(2, minmax(0, 1fr)) !important;
    }}
  }}

  @media (max-width: 520px) {{
    .bq-page-title {{
      font-size: 1.65rem;
    }}

    .bq-toolbar,
    .bq-control-row {{
      gap: 0.5rem;
    }}

    .bq-data-table .q-table {{
      min-width: 560px;
    }}

    .bq-chat-bubble {{
      max-width: 94%;
    }}
  }}
</style>
""",
        shared=True,
    )
