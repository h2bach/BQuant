# BQuant Current Data Architecture

Snapshot date: `2026-06-24`

## 1. Data architecture goals

The current BQuant data architecture is designed around three practical goals:

1. keep a canonical local warehouse in DuckDB
2. keep plot-facing datasets as one Parquet file per symbol
3. separate stable base data from short-lived intraday delta data

This matches the current VN30-first scope:

- `10-year` daily OHLCV base
- `60-day` intraday `15m` base
- separate intraday `15m` delta until end-of-day merge

## 2. Physical storage layout

### 2.1 Main storage roots

| Path | Purpose |
| --- | --- |
| `warehouse/bquant.duckdb` | main analytical and control-plane warehouse |
| `warehouse/bquant_observability.duckdb` | observability warehouse |
| `data/base/` | canonical plot-facing Parquet datasets |
| `data/raw/` | raw ingestion landing area |
| `data/silver/` | cleaned datasets |
| `data/gold/` | feature datasets |
| `data/marts/` | serving / output datasets |
| `data/metadata/` | manifest parquet and metadata outputs |
| `logs/observability/` | structured operational event logs |

### 2.2 Plot-facing base dataset paths

| Dataset | DuckDB table | Parquet path | File layout |
| --- | --- | --- | --- |
| `daily_ohlcv_10y` | `daily_ohlcv_base` | `data/base/daily_10y` | one file per symbol |
| `intraday_ohlcv_15m_60d` | `intraday_ohlcv_15m_base` | `data/base/intraday_15m_60d` | one file per symbol |
| `intraday_ohlcv_15m_delta` | `intraday_ohlcv_15m_delta` | `data/base/intraday_15m_delta` | one file per symbol |
| `data_file_manifest` | `data_file_manifest` | `data/metadata/data_file_manifest.parquet` | single metadata file |

## 3. Canonical dataset registry

The active registry is defined in:

- [configs/dataset_registry.yaml](/storage/hhbach/bquant/configs/dataset_registry.yaml:1)
- [configs/data_management.yaml](/storage/hhbach/bquant/configs/data_management.yaml:1)
- [configs/storage.yaml](/storage/hhbach/bquant/configs/storage.yaml:1)

Current plot-facing dataset contract:

| Dataset | Source | Time grain | Write mode | File pattern |
| --- | --- | --- | --- | --- |
| `daily_ohlcv_10y` | `yfinance` | daily | `replace_per_symbol` | `{symbol}_{coverage_start}_curr.parquet` |
| `intraday_ohlcv_15m_60d` | `yfinance` | intraday 15m | `replace_per_symbol` | `{symbol}_intra60_{snapshot_date}.parquet` |
| `intraday_ohlcv_15m_delta` | `yfinance` | intraday 15m | `replace_per_symbol` | `{symbol}_intra_delta_{snapshot_date}.parquet` |
| `data_file_manifest` | system | metadata | `replace` | single parquet file |

## 4. Main warehouse schema

Schema source:

- [warehouse/schema_duckdb.sql](/storage/hhbach/bquant/warehouse/schema_duckdb.sql:1)

### 4.1 Universe and metadata tables

| Table | Grain / PK | Purpose |
| --- | --- | --- |
| `universe_members` | `(universe_name, symbol, effective_date)` | canonical VN30 membership tracking |
| `metadata` | `key` | repository / schema metadata |

### 4.2 Raw landing tables

| Table | Grain / PK | Purpose |
| --- | --- | --- |
| `raw_ohlcv` | no enforced PK | raw daily OHLCV landing |
| `raw_ohlcv_hourly` | `(symbol, bar_time, source)` | raw hourly OHLCV landing |

These exist in the schema but are not the main active path for the current charting workflow.

### 4.3 Clean / silver tables

| Table | Grain / PK | Purpose |
| --- | --- | --- |
| `clean_ohlcv_daily` | `(symbol, trading_date)` | cleaned daily OHLCV |
| `clean_ohlcv_hourly` | `(symbol, bar_time)` | cleaned hourly OHLCV |

### 4.4 Base market data tables

| Table | Grain / PK | Purpose |
| --- | --- | --- |
| `daily_ohlcv_base` | `(symbol, trading_date)` | canonical 10-year daily base |
| `intraday_ohlcv_15m_base` | `(symbol, bar_time)` | canonical 60-day intraday base |
| `intraday_ohlcv_15m_delta` | `(symbol, bar_time)` | current-session or post-snapshot intraday delta |

Key base-table semantics:

- `daily_ohlcv_base` is the source for daily symbol charts
- `intraday_ohlcv_15m_base` is the source for 60-day intraday charts
- `intraday_ohlcv_15m_delta` is intentionally separate until the end-of-day merge

### 4.5 Control-plane tables

| Table | Grain / PK | Purpose |
| --- | --- | --- |
| `data_file_manifest` | `(dataset_name, symbol, file_role)` | one current metadata row per dataset-symbol-role |
| `pipeline_runs` | `run_id` | local warehouse pipeline run history |
| `dataset_refresh_state` | `dataset_name` | cache invalidation and freshness versioning |

### 4.6 Feature and serving tables

| Table | Grain / PK | Purpose |
| --- | --- | --- |
| `technical_features_daily` | `(symbol, trading_date)` | technical indicators |
| `combined_features_daily` | `(symbol, trading_date)` | combined model features |
| `trading_signals` | `signal_id` | generated signals |
| `backtest_runs` | `run_id` | backtest summaries |

These tables exist in the schema and are part of the intended platform model, but the current active implementation focus is still the base market-data stack.

## 5. Core analytical views

Important views already implemented:

| View | Purpose |
| --- | --- |
| `v_universe_members` | current universe membership filter |
| `v_daily_ohlcv_base_universe` | daily base rows restricted to active VN30 symbols |
| `v_intraday_15m_base_universe` | intraday base rows restricted to active VN30 symbols |
| `v_intraday_15m_plot_universe` | plot-facing intraday view with delta merged on read |
| `v_data_file_manifest_current` | current manifest rows |
| `v_latest_signals` | latest signal summary |
| `v_technical_features_universe` | VN30-scoped technical features |
| `v_combined_features_universe` | VN30-scoped combined features |
| `v_backtest_summary` | backtest serving view |

The most important current plot view is:

```sql
v_intraday_15m_plot_universe
  = intraday_ohlcv_15m_base
    UNION ALL
    intraday_ohlcv_15m_delta rows not already present in base
```

That design lets the UI see a merged logical dataset without forcing an immediate physical merge.

## 6. File naming and actual file layout

### 6.1 Naming conventions

Current naming rules:

- daily base:
  - `{symbol}_{coverage_start}_curr.parquet`
- intraday 60d base:
  - `{symbol}_intra60_{snapshot_date}.parquet`
- intraday delta:
  - `{symbol}_intra_delta_{snapshot_date}.parquet`

### 6.2 Real examples in the repository

Observed files:

- `data/base/daily_10y/BID_20160608_curr.parquet`
- `data/base/daily_10y/ACB_20201209_curr.parquet`
- `data/base/intraday_15m_60d/BID_intra60_20260624.parquet`
- `data/base/intraday_15m_60d/VNM_intra60_20260624.parquet`

At inspection time, the delta folder has no active files populated through live update yet.

## 7. Manifest model

Manifest implementation:

- [warehouse/data_manifest.py](/storage/hhbach/bquant/warehouse/data_manifest.py:1)

Current key fields in `data_file_manifest`:

| Field | Meaning |
| --- | --- |
| `dataset_name` | logical dataset id |
| `symbol` | ticker |
| `file_role` | `base` or `delta` |
| `granularity` | `daily` or `intraday` |
| `interval` | dataset interval such as `1d` or `15m` |
| `file_name` | emitted file name |
| `file_path` | absolute file path |
| `coverage_start` | first timestamp/date in the file |
| `coverage_end` | last timestamp/date in the file |
| `row_count` | row count |
| `snapshot_date` | snapshot marker for intraday files |
| `latest_expected_ts` | freshness expectation anchor |
| `update_status` | freshness state |
| `needs_merge` | whether data is pending base merge |
| `last_refresh_at` | last materialization time |

Manifest status values from config:

- `up_to_date`
- `stale`
- `awaiting_refresh_window`
- `pending_merge`
- `empty`
- `unknown`

Current behavior:

- daily base rows become `stale` once the expected current date is ahead of `coverage_end`
- intraday delta rows should become `pending_merge`
- empty materialization can be represented explicitly

## 8. Refresh-version model

Refresh-version helpers:

- [warehouse/refresh_state.py](/storage/hhbach/bquant/warehouse/refresh_state.py:1)

`dataset_refresh_state` is the current cache invalidation contract between pipelines and the web app.

Tracked fields:

| Field | Meaning |
| --- | --- |
| `dataset_name` | logical dataset |
| `refresh_version` | monotonically increasing cache token |
| `last_success_at` | last successful refresh time |
| `latest_data_ts` | latest data timestamp known for the dataset |
| `last_run_id` | pipeline run that produced the refresh |
| `updated_at` | state update time |

Current design intent:

- pipelines bump refresh version only after a real successful data update
- web chart caches include refresh version in the cache key
- worker and web can run as separate processes without requiring a web restart

## 9. Observability data model

Observability schema source:

- [warehouse/observability_schema.sql](/storage/hhbach/bquant/warehouse/observability_schema.sql:1)

### 9.1 Operational tables

| Table | Purpose |
| --- | --- |
| `obs_log_events` | raw structured event projection from JSONL |
| `obs_job_runs` | pipeline run projection |
| `obs_source_requests` | source request timing, wait, empty-payload, and error projection |
| `obs_live_checkpoints` | per-dataset per-symbol watermarks |
| `obs_scheduler_heartbeats` | worker heartbeat stream |
| `obs_alerts` | alert state table |
| `obs_ingestion_offsets` | idempotent JSONL ingest offsets |
| `obs_dead_letter_events` | malformed JSONL lines |

### 9.2 Operational views

| View | Purpose |
| --- | --- |
| `v_obs_active_alerts` | currently open / acknowledged alerts |
| `v_obs_recent_failures` | recent `ERROR` and `CRITICAL` events |
| `v_obs_latest_job_status` | latest state per pipeline |
| `v_obs_dataset_freshness` | dataset-level checkpoint summary |
| `v_obs_symbol_lag` | per-symbol checkpoint summary |
| `v_obs_worker_health` | latest heartbeat per worker |

## 10. Current data snapshot

Observed from the current warehouse state on `2026-06-24`:

### 10.1 Main warehouse row counts

| Table / view | Row count |
| --- | --- |
| `daily_ohlcv_base` | `68,218` |
| `intraday_ohlcv_15m_base` | `26,962` |
| `intraday_ohlcv_15m_delta` | `0` |
| `data_file_manifest` | `60` |
| `dataset_refresh_state` | `0` rows |
| `pipeline_runs` | `15` |
| `v_intraday_15m_plot_universe` | `26,962` |

### 10.2 File-manifest dataset snapshot

Observed grouped state:

| Dataset | Files | Coverage start | Coverage end | Status |
| --- | --- | --- | --- | --- |
| `daily_ohlcv_10y` | `30` | `2016-06-08` | `2026-06-08` | all `stale` |
| `intraday_ohlcv_15m_60d` | `30` | `2026-03-17 09:15` | `2026-06-08 11:15` | all `stale` |

### 10.3 Observability snapshot

| Table | Row count |
| --- | --- |
| `obs_log_events` | `62` |
| `obs_job_runs` | `9` |
| `obs_source_requests` | `0` |
| `obs_live_checkpoints` | `0` |
| `obs_scheduler_heartbeats` | `1` |
| `obs_alerts` | `3` |

Observed alert state:

- `daily_eod_missing`: open
- `intraday_dataset_lag`: open
- `worker_heartbeat_lag`: resolved

### 10.4 Refresh-state snapshot

Current tracked datasets are present logically in the code, but the table still has no stored rows. In practice this means:

- `daily_ohlcv_10y` refresh version is still logically `0`
- `intraday_ohlcv_15m_60d` refresh version is still logically `0`
- `intraday_ohlcv_15m_delta` refresh version is still logically `0`

## 11. Current data lineage

### 11.1 Plot-facing market data lineage

```text
yfinance
  -> daily_ohlcv_base
  -> per-symbol daily parquet files
  -> data_file_manifest
  -> daily chart views / pages

yfinance
  -> intraday_ohlcv_15m_base
  -> per-symbol intraday base parquet files
  -> data_file_manifest

yfinance live delta
  -> intraday_ohlcv_15m_delta
  -> per-symbol intraday delta parquet files
  -> data_file_manifest
  -> v_intraday_15m_plot_universe
  -> intraday chart views / pages
```

### 11.2 Observability lineage

```text
pipelines / web / worker
  -> structured JSONL files
  -> ingest_observability_logs
  -> observability DuckDB tables
  -> operations page / alerts page
```

## 12. Current data gaps

1. Base datasets are stale relative to the repository date.
2. Delta dataset is currently empty.
3. No real source-request rows have been projected into observability yet.
4. No live checkpoints exist yet.
5. `dataset_refresh_state` exists in schema and code, but has not been populated by successful live-update runs yet.
6. Raw / silver / gold / marts layers are schema-defined, but the currently active user-facing workflow is mostly concentrated on the base market-data layer plus observability.
