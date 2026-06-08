# Skill: Warehouse and Lakehouse

## Purpose

Use this skill whenever working with storage, schemas, DuckDB bindings, or Parquet layout.

## MVP Storage Decision

For the MVP, do not use PostgreSQL as the primary warehouse.

Use:

- DuckDB database file: `warehouse/bquant.duckdb`
- Parquet data lake: `data/raw/`, `data/silver/`, `data/gold/`, `data/marts/`

ClickHouse is a later extension for realtime or intraday analytics only.

## Data Lake Architecture

BQuant uses a DuckDB + Parquet Data Lake architecture:

```text
data/
├── raw/                    # Raw or near-raw collected data from sources
│   └── ohlcv/
│       └── source=vnquant/
│           └── symbol=<SYMBOL>/*.parquet
├── silver/                 # Cleaned and standardized datasets
│   ├── clean_ohlcv_daily/*.parquet
│   └── universe_members/*.parquet
├── gold/                   # Feature datasets
│   ├── technical_features_daily/*.parquet
│   └── combined_features_daily/*.parquet
└── marts/                  # Consumption-ready datasets for dashboard/backtest
    ├── trading_signals/*.parquet
    └── backtest_results/*.parquet

warehouse/
└── bquant.duckdb           # DuckDB database file for querying and analysis
```

## Core DuckDB Objects

Create and maintain these tables or equivalent views in `warehouse/bquant.duckdb`:

- `universe_members`
- `raw_ohlcv`
- `clean_ohlcv_daily`
- `technical_features_daily`
- `combined_features_daily`
- `trading_signals`
- `backtest_runs`
- `pipeline_runs`

## Required Warehouse Modules

`warehouse/duckdb_connection.py`

Expected functions:

```python
def get_duckdb_path() -> str: ...
def get_connection(read_only: bool = False): ...
def execute_sql_file(sql_path: str) -> None: ...
def query_df(sql: str): ...
def write_df(table_name: str, df, mode: str = "append") -> None: ...
```

`warehouse/parquet_io.py`

Expected functions:

```python
def write_parquet(df, path: str, partition_cols: list[str] | None = None) -> None: ...
def read_parquet(path: str): ...
def export_table_to_parquet(table_name: str, output_path: str) -> None: ...
```

## Dataset Registry

All dataset paths must be registered in `configs/dataset_registry.yaml`. Do not hard-code paths in code.

Example registry:

```yaml
datasets:
  raw_ohlcv:
    parquet_path: "data/raw/ohlcv/source=vnquant"
    duckdb_table: "raw_ohlcv"
    layer: "raw"
  
  clean_ohlcv_daily:
    parquet_path: "data/silver/clean_ohlcv_daily"
    duckdb_table: "clean_ohlcv_daily"
    layer: "silver"
  
  universe_members:
    parquet_path: "data/silver/universe_members"
    duckdb_table: "universe_members"
    layer: "silver"
  
  technical_features_daily:
    parquet_path: "data/gold/technical_features_daily"
    duckdb_table: "technical_features_daily"
    layer: "gold"
  
  combined_features_daily:
    parquet_path: "data/gold/combined_features_daily"
    duckdb_table: "combined_features_daily"
    layer: "gold"
  
  trading_signals:
    parquet_path: "data/marts/trading_signals"
    duckdb_table: "trading_signals"
    layer: "marts"
  
  backtest_results:
    parquet_path: "data/marts/backtest_results"
    duckdb_table: "backtest_runs"
    layer: "marts"
```

## Schema Logic

Define schema logic in `warehouse/schema_duckdb.sql` for all core tables. Each table must have:
- Column definitions with data types
- Primary key or logical key
- Index columns for performance
- created_at timestamp for audit trail

## Rules

- Save durable dataset copies to Parquet in appropriate layer (raw/silver/gold/marts).
- Keep raw and cleaned layers separate.
- Append or upsert raw market data when practical.
- Rebuild or replace derived tables (silver/gold/marts) when needed.
- Keep SQL schema files under `warehouse/`, not inside dashboards or notebooks.
- Use DuckDB and Parquet directly in MVP; do not add Postgres-only assumptions.
- All dataset paths must come from configs/dataset_registry.yaml, never hard-coded.
- DuckDB database file is warehouse/bquant.duckdb.
- Use DuckDB for querying, analysis, joins, and serving dashboard/backtest.
- Use Parquet as the primary durable storage format.
- Log all data operations to logs/data/ with structured JSONL format.
