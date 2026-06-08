# Skill: Data Ingestion

## Purpose

Use this skill whenever building or updating market data ingestion for BQuant.

## Scope

MVP ingestion covers:

1. VN30 universe membership
2. Historical OHLCV from `vnquant`
3. Optional benchmark or index data
4. OHLCV validation and cleaning

## Required Modules

- `data_ingestion/fetch_vn30_universe.py`
- `data_ingestion/fetch_vnquant_ohlcv.py`
- `data_ingestion/fetch_index_data.py`
- `data_ingestion/validate_ohlcv.py`

## Standard OHLCV Output

Normalize source data to:

```text
symbol
trading_date
open
high
low
close
adjusted_close
volume
source
```

## Validation Rules

- no missing `symbol`
- valid `trading_date`
- numeric OHLCV fields
- `high >= low`
- `volume >= 0`
- remove duplicates by `symbol + trading_date`
- sort by `symbol + trading_date`

## Storage Rules

- Save raw source output to Bronze.
- Save cleaned daily data to Silver.
- Export cleaned OHLCV to Parquet and DuckDB.
- Keep source-specific API logic isolated inside the adapter module.

## Implementation Notes

- If the exact `vnquant` API is uncertain, keep the uncertainty inside one adapter function.
- Add a small validation script or notebook for a single symbol such as `FPT` before broad ingestion.
