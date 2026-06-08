# Skill: Dashboard and Reporting

## Purpose

Use this skill whenever building user-facing review surfaces for signals, features, or backtests.

## Required Module

- `dashboard/streamlit_app.py`

## Dashboard Responsibilities

The dashboard should read from DuckDB and show:

1. latest trading signals
2. top `BUY` candidates
3. `AVOID` or high-risk candidates
4. backtest result table
5. feature summary by symbol
6. later, LLM explanations

## Rules

- Keep business logic outside Streamlit.
- Read precomputed tables or views instead of recomputing pipelines in the UI.
- Handle empty datasets and partial pipeline states cleanly.
- Do not add order execution or broker actions.
- Prefer fast summary queries over heavy ad hoc recomputation.

## Reporting Notes

- Backtest summaries belong in `backtesting/reports.py` or similar helpers.
- Persist dashboard-ready tables before rendering when the query logic becomes non-trivial.
