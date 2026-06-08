# Codex Instructions for BQuant

You are assisting with the development of BQuant, a quantitative research pipeline for the Vietnam stock market, starting with the `VN30` universe.

## Main Goals

Build a modular system that supports:

1. Conda-based environment setup
2. Historical OHLCV ingestion using `vnquant`
3. DuckDB + Parquet lakehouse storage
4. Bronze/Silver/Gold data organization
5. Technical feature engineering
6. Baseline backtesting
7. Rule-based signal generation
8. Streamlit dashboard
9. Later ML, NLP, and LLM extensions

## Primary Technology Decisions

- Python `3.10`
- Conda for environment management
- DuckDB as the MVP analytical warehouse
- Parquet as the durable storage format
- Pandas and/or Polars for transformations
- `ta`, `pandas-ta`, or custom functions for indicators
- `vectorbt` or a simple custom engine for baseline backtests
- Streamlit for dashboard runtime
- FastAPI only as a later integration surface
- ClickHouse only for future realtime or intraday use cases

## Core Engineering Rules

- Keep code modular and testable.
- Use clear module boundaries.
- Prefer the simplest correct implementation first.
- Add docstrings to important functions.
- Use logging where execution matters.
- Avoid large monolithic scripts.

## Data Rules

- Do not hard-code credentials.
- Use `.env` for secrets and external configuration.
- Preserve raw data whenever possible.
- Always validate OHLCV before feature computation.
- Always sort by `symbol` and `trading_date` for time-series work.
- Avoid look-ahead bias and data leakage.

## Trading and Modeling Rules

- This system is for research and decision support.
- Do not implement real-money live trading in MVP.
- Do not use target labels to generate live signals.
- Do not randomly shuffle time-series data for validation.
- Always account for transaction costs in backtests.
- All explanations must state that the output is not financial advice.

## Implementation Order

Build in this order:

1. project skeleton and environment
2. DuckDB utilities and schema
3. VN30 universe loader
4. OHLCV ingestion and validation
5. technical features
6. combined features and labels
7. baseline backtest
8. signal generation
9. dashboard
10. tests
11. later extensions
