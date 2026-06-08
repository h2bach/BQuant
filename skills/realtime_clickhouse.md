# Skill: Realtime and ClickHouse

## Purpose

Use this skill only when BQuant expands into realtime or intraday analytics.

## Status

This is a future extension. The MVP must not depend on ClickHouse.

## When to Use ClickHouse

Use ClickHouse for:

- intraday bars
- tick or quote snapshots
- high-volume append-only market data
- low-latency dashboard queries on larger datasets

Do not add it just to store daily MVP research tables.

## Planned Files

- `warehouse/clickhouse_schema.sql`
- `warehouse/clickhouse_client.py`
- `pipelines/realtime_ingestion_pipeline.py`

## Suggested Core Tables

- `intraday_bars`
- `market_snapshots`

## Rules

- Keep ClickHouse-specific logic isolated from DuckDB MVP logic.
- Do not fork the whole pipeline just to support one future backend.
- Preserve the DuckDB + Parquet path as the local research baseline.
