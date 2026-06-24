# BQuant Build Report

**Date**: 2026-06-08  
**System**: BQuant - Quantitative Research Pipeline for Vietnam Stock Market  
**Build Phase**: Base Dataset Platform Ready, Source-Only Packaging Prepared

---

## Executive Summary

This report captures the current implementation state after completing the executable base data platform for BQuant.

The repository has moved past architecture-only setup and now includes working ingestion, storage, file materialization, logging, and integrity validation for the two canonical base datasets:

- VN30 daily OHLCV, 10-year horizon
- VN30 intraday OHLCV, 15-minute interval, latest 60-day snapshot

The codebase has also been prepared for GitHub upload in source-only form by defining ignore rules for runtime data, logs, database files, and other generated artifacts.

---

## Current Runtime Status

### Canonical base datasets

1. `daily_ohlcv_10y`
   - provider: `yfinance`
   - storage model: one parquet file per symbol
   - filename pattern: `{symbol}_{coverage_start}_curr.parquet`
   - DuckDB table: `daily_ohlcv_base`

2. `intraday_ohlcv_15m_60d`
   - provider: `yfinance`
   - storage model: one parquet file per symbol
   - filename pattern: `{symbol}_intra60_{snapshot_date}.parquet`
   - DuckDB table: `intraday_ohlcv_15m_base`

3. `intraday_ohlcv_15m_delta`
   - scaffolded for forward updates
   - not populated yet

4. `data_file_manifest`
   - tracks file coverage, status, refresh timestamps, and file paths

### Verified local dataset counts

- VN30 symbols in `universe_members`: `30`
- Daily base rows: `68,218`
- Daily base symbols: `30`
- Intraday base rows: `26,962`
- Intraday base symbols: `30`
- Manifest rows: `60`

### Manifest status interpretation

- `up_to_date`: `30`
- `awaiting_refresh_window`: `30`

The `awaiting_refresh_window` rows are expected for the intraday base snapshot before the configured end-of-day refresh boundary at `15:00`, not a data failure.

---

## Implemented Components

### Storage and warehouse runtime

- `warehouse/schema_duckdb.sql`
- `warehouse/duckdb_connection.py`
- `warehouse/parquet_io.py`
- `warehouse/init_duckdb.py`
- `warehouse/data_manifest.py`

### Logging and execution utilities

- `utils/logger.py`
- `utils/rate_limit.py`

### Ingestion modules

- `data_ingestion/fetch_vn30_universe.py`
- `data_ingestion/yfinance_adapter.py`
- `data_ingestion/fetch_daily_10y_base.py`
- `data_ingestion/fetch_intraday_15m.py`

### Configuration layer

- `configs/bquant.yaml`
- `configs/storage.yaml`
- `configs/data_sources.yaml`
- `configs/data_management.yaml`
- `configs/dataset_registry.yaml`
- `configs/universe_vn30.yaml`

### Validation and inspection support

- `data/data_integrity_audit.ipynb`

---

## Integrity and Quality Status

### Notebook audit

The audit notebook now checks:

- dataset inventory against the registry
- file-vs-table-vs-manifest consistency
- daily and intraday OHLCV integrity constraints
- direct parquet inspection by symbol/file

### Fixed during this phase

1. Notebook failure caused by `NaN -> int` conversion in the issue summary cell
2. Daily OHLC bound anomalies caused by provider-returned inconsistent high/low values
3. Materialization conflict in DuckDB caused by mixed concurrent connection modes

### Current result

- no remaining `critical` issue in the active base-data path
- only `info` rows remain for future datasets that are registered but not built yet

---

## Source-of-Truth Decisions

### Provider decision

The canonical base path is now built around `yfinance`.

- `vnquant` remains only as a legacy experiment path
- `vnstock` remains only as a legacy experiment path
- production source abstraction is now centered on the `yfinance_adapter`

### Data layout decision

- daily: one file per symbol
- intraday base: one file per symbol
- intraday forward updates: separate delta file per symbol
- manifest: single metadata parquet plus DuckDB metadata table

This layout was chosen to keep chart-building and per-symbol read paths simple and predictable.

---

## Logging Status

Structured runtime logging is active for:

- warehouse initialization
- universe load
- daily base backfill
- intraday base backfill

Log outputs are written locally to:

- `logs/data/ingestion/`
- `logs/pipeline/`
- `logs/errors/`

These are runtime artifacts and are not intended for GitHub upload.

---

## GitHub Packaging Status

### Prepared now

- source code and configuration kept
- runtime data excluded
- logs excluded
- DuckDB database files excluded
- generated reports excluded
- notebook outputs can be stripped so sample data is not embedded in the repo artifact

### Important note

This workspace does not currently contain a `.git/` directory, so it is not yet an initialized Git repository. The ignore rules are still ready to use as soon as the repository is initialized or copied into an existing Git checkout.

---

## Recommended Next Phase

1. Build daily refresh job for the latest bar after `15:00`
2. Build intraday delta updater
3. Add merge job from `intraday_ohlcv_15m_delta` into the canonical intraday base snapshot
4. Start the silver cleaning path and feature generation path on top of the current base datasets

---

## Status

**Base data platform**: implemented and verified  
**Base dataset integrity**: verified for the active datasets  
**GitHub source-only packaging**: prepared
