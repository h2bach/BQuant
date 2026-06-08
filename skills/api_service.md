# Skill: API Service

## Purpose

Use this skill whenever exposing BQuant outputs through programmatic interfaces.

## Scope

API work is a later integration surface, not the first implementation target.

## Planned Files

- `api/main.py`
- `api/routes/`

## API Responsibilities

- expose latest signals
- expose backtest summaries
- expose feature summaries by symbol
- expose health or readiness checks

## Rules

- Keep the API thin.
- Read from warehouse tables, views, or service helpers.
- Do not re-run ingestion, feature building, or backtests inside request handlers.
- Do not expose broker execution endpoints in MVP.
