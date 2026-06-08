# Skill: Project Context

## Purpose

Use this skill whenever you need the high-level scope, market context, or guardrails for BQuant.

## Project Summary

BQuant is a quantitative research and decision-support pipeline for the Vietnam stock market.

- System name: `BQuant`
- Initial universe: `VN30`
- MVP frequency: daily bars
- Initial data source: `vnquant`
- MVP storage: `DuckDB + Parquet Data Lake`
  - Parquet: durable storage in data/raw, data/silver, data/gold, data/marts
  - DuckDB: analytical engine at warehouse/bquant.duckdb
- Later realtime option: `ClickHouse`

The first working target is:

`VN30 + vnquant OHLCV + DuckDB + Parquet Data Lake + technical features + baseline backtest + signal dashboard`

## MVP Scope

Implement these first:

1. Conda environment
2. Project structure
3. VN30 universe config and loader
4. OHLCV ingestion
5. OHLCV validation and cleaning
6. Bronze/Silver/Gold storage
7. Technical features
8. Combined features and labels
9. Baseline backtesting
10. Rule-based signal generation
11. Streamlit dashboard
12. Basic tests

## Architecture Direction

BQuant uses a DuckDB + Parquet Data Lake architecture:

- **data/raw**: Raw or near-raw collected data from sources (vnquant OHLCV, etc.)
- **data/silver**: Cleaned and standardized datasets (clean OHLCV, universe members)
- **data/gold**: Feature datasets (technical features, combined features)
- **data/marts**: Consumption-ready datasets (trading signals, backtest results, dashboard data)
- **warehouse/bquant.duckdb**: DuckDB database file for querying, analysis, joins, and serving dashboard/backtest

Parquet is the primary durable storage format. DuckDB is the analytical engine that can query Parquet files directly and maintain views/tables for efficient access.

## Non-Goals for MVP

Do not implement these before the core OHLCV pipeline is stable:

- live trading
- broker order placement
- mandatory realtime infra
- complex deep learning
- full NLP pipeline
- LLM-generated decisions

## Key Constraints

- This system is for research and decision support, not direct investment advice.
- Do not hard-code credentials or API keys.
- Do not overwrite raw data without clear intent.
- Avoid look-ahead bias and target leakage.
- Keep modules small, testable, and reusable.
- All data operations must be logged to logs/data/ with structured JSONL format.
- Follow the implementation order: storage architecture → schema → data pipelines.
