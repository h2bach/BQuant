# BQuant Current System Architecture

Snapshot date: `2026-06-29`

## 1. Purpose and current scope

BQuant is currently implemented as a local quantitative research and decision-support platform for the Vietnam stock market, with the VN30 universe as the initial scope.

The system already includes:

- a local web application for data exploration and monitoring
- a DuckDB-based warehouse for market data and derived datasets
- a dbt-on-DuckDB transformation layer for tested analytics and agent-ready marts
- a separate DuckDB-based observability store
- local CLI pipelines for base data refresh, live-update preparation, manifest refresh, and alerts
- structured logging with JSONL fan-out and later ingestion into the observability database

The system is still in a local-development stage. The live-update layer is implemented, but has only been validated with `dry-run` and `no_data` scenarios so far.

## 2. Runtime model

BQuant currently runs as a single-repository, single-machine system.

Main runtime assumptions:

- execution mode: local
- Python environment: `conda env bquant`
- web app: NiceGUI on top of FastAPI
- primary storage: DuckDB + Parquet
- logs: local text + JSONL files
- observability ingestion: single writer into the observability DB

Current process topology:

```text
User
  |
  v
NiceGUI / FastAPI web app
  |                         \
  | read                     \ manual actions
  v                           v
Main DuckDB <------------ CLI pipelines / worker
  |                              |
  | materialize                  | structured JSONL logs
  v                              v
Parquet files                 logs/observability/jsonl/*
  |
  v
dbt DuckDB transforms
  |
  v
analytics_staging / analytics_intermediate / analytics_marts
                                     |
                                     v
                           ingest_observability_logs
                                     |
                                     v
                        Observability DuckDB + alert views
```

## 3. Top-level repository structure

The parts that are active in the current system are:

| Path | Role |
| --- | --- |
| `apps/web/` | NiceGUI web application |
| `data_ingestion/` | source adapters and ingestion scripts |
| `pipelines/` | orchestration entrypoints for refresh, live update, ingest, alerts |
| `warehouse/` | DuckDB schema, connections, manifest, refresh-state, observability init |
| `transformations/dbt/` | dbt project for DuckDB staging, intermediate models, data tests, and marts |
| `configs/` | system, storage, dataset, live-update, and observability configuration |
| `data/` | Parquet lake layout |
| `logs/` | text logs and structured JSONL logs |
| `tests/` | current live-update resilience tests |
| `architectures/` | current architecture documentation |

## 4. Main system components

### 4.1 Web application

Entry point:

- [apps/web/main.py](/storage/hhbach/bquant/apps/web/main.py:1)

Current routes:

| Route | File | Purpose |
| --- | --- | --- |
| `/` | `apps/web/pages/dashboard.py` | market overview dashboard |
| `/catalog` | `apps/web/pages/catalog.py` | dataset catalog |
| `/symbol` | `apps/web/pages/symbol_explorer.py` | per-symbol charting and inspection |
| `/manifest` | `apps/web/pages/manifest.py` | file-manifest view |
| `/quality` | `apps/web/pages/quality.py` | data quality view |
| `/sql_lab` | `apps/web/pages/sql_lab.py` | ad hoc SQL exploration |
| `/operations` | `apps/web/pages/operations.py` | worker, job, freshness, and error monitoring |
| `/alerts` | `apps/web/pages/alerts.py` | alert monitoring and status review |

Key services:

- [apps/web/services/charting.py](/storage/hhbach/bquant/apps/web/services/charting.py:1)
- [apps/web/services/operations.py](/storage/hhbach/bquant/apps/web/services/operations.py:1)

Important charting behavior:

- daily and intraday chart loads are cached with `lru_cache`
- cache keys include `refresh_version` from `dataset_refresh_state`
- VNIndex, VN30, and symbol charts render with TradingView Lightweight Charts
- chart overlays include EMA/SMA, Bollinger Bands, Donchian Channels, Keltner Channels, VWAP where applicable, and Supertrend
- lower signal panels include normalized momentum, volatility, trend-strength, and volume-flow indicators: RSI, Stochastic, Williams %R, ROC, CCI, MACD, ATR%, ADX/DI, Aroon, MFI, CMF, OBV, and Bollinger width
- intraday symbol chart reads from `v_intraday_15m_plot_universe`, which merges base and delta rows on read

### 4.2 Main warehouse

Primary database:

- `warehouse/bquant.duckdb`

Schema bootstrap:

- [warehouse/schema_duckdb.sql](/storage/hhbach/bquant/warehouse/schema_duckdb.sql:1)
- [warehouse/init_duckdb.py](/storage/hhbach/bquant/warehouse/init_duckdb.py:1)

Main responsibilities:

- hold canonical VN30 data tables
- expose plot-oriented views
- store pipeline run history
- store `dataset_refresh_state`
- back Parquet materialization metadata via `data_file_manifest`

### 4.3 Parquet data lake

Primary paths:

- `data/base/daily_10y`
- `data/base/intraday_15m_60d`
- `data/base/intraday_15m_delta`
- `data/metadata/data_file_manifest.parquet`

Design rule currently implemented:

- one file per symbol for plot-facing base datasets
- delta kept separate from base until end-of-day reconcile

Materialization logic:

- [warehouse/data_manifest.py](/storage/hhbach/bquant/warehouse/data_manifest.py:1)

### 4.4 Live-update runtime

Runtime helpers:

- [pipelines/live_update_runtime.py](/storage/hhbach/bquant/pipelines/live_update_runtime.py:1)

Current entrypoints:

| Entrypoint | File | Purpose |
| --- | --- | --- |
| `python -m pipelines.live_update_worker` | `pipelines/live_update_worker.py` | long-running local worker |
| `python -m pipelines.run_intraday_delta` | `pipelines/run_intraday_delta.py` | fetch and upsert 15m delta bars |
| `python -m pipelines.run_eod_reconcile` | `pipelines/run_eod_reconcile.py` | refresh latest daily bar, merge delta into base |
| `python -m pipelines.refresh_manifest` | `pipelines/refresh_manifest.py` | rebuild manifest rows |
| `python -m pipelines.ingest_observability_logs` | `pipelines/ingest_observability_logs.py` | JSONL -> observability DB projection |
| `python -m pipelines.evaluate_alerts` | `pipelines/evaluate_alerts.py` | evaluate alert rules and state transitions |

Current scheduling contract from config:

- timezone: `Asia/Ho_Chi_Minh`
- intraday cadence: 15 minutes
- slots: `09:15` through `14:45`
- worker poll interval: `60s`
- EOD reconcile cutoff: `15:10`
- catch-up window after restart: `2` slots

Important runtime behavior already implemented:

- worker supports `--once` and `--dry-run`
- intraday and EOD jobs support `--dry-run`
- jobs return `status=no_data` when source data is empty, instead of pretending success
- worker records heartbeat and scheduling decisions

### 4.5 Observability subsystem

Observability database:

- `warehouse/bquant_observability.duckdb`

Schema and bootstrap:

- [warehouse/observability_schema.sql](/storage/hhbach/bquant/warehouse/observability_schema.sql:1)
- [warehouse/init_observability.py](/storage/hhbach/bquant/warehouse/init_observability.py:1)

Structured logging:

- [utils/logger.py](/storage/hhbach/bquant/utils/logger.py:1)

Current logging model:

- each component writes structured JSONL immediately
- logs are separated by channel:
  - `scheduler`
  - `pipeline`
  - `ingestion`
  - `web`
  - `alerts`
- `ingest_observability_logs` is the intended single writer into the observability DB

Current operational rule:

- DuckDB write concurrency is a known constraint
- web should read observability data read-only
- direct concurrent multi-writer behavior should be avoided

### 4.6 Data ingestion layer

Active ingestion code:

| File | Purpose |
| --- | --- |
| `data_ingestion/vnstock_adapter.py` | current daily market-data adapter |
| `data_ingestion/fetch_daily_10y_base.py` | daily base data loading |
| `data_ingestion/fetch_intraday_15m.py` | intraday 15m loading |
| `data_ingestion/fetch_vn30_universe.py` | VN30 symbol universe |
| `data_ingestion/fetch_vnquant_ohlcv.py` | older raw daily path |
| `data_ingestion/fetch_vnstock_hourly_ohlcv.py` | older raw hourly path |
| `data_ingestion/validate_ohlcv.py` | validation utilities |

Current source posture:

- `VCI-data-source` is the active `vnstock` provider for the daily base and market-index datasets
- The Python API still uses `source="VCI"` because that is the provider code expected by `vnstock`; it is not the `VCI` stock symbol.
- `vnquant` and `vnstock` code still exists in the repo, mainly as historical/raw-layer support

## 5. End-to-end flows

### 5.1 Bootstrap flow

```text
init_duckdb
  -> create directories
  -> execute warehouse/schema_duckdb.sql
  -> initialize manifest parquet
  -> create dataset_refresh_state
  -> initialize observability DB
  -> verify expected tables and views
```

### 5.2 Base data refresh and file materialization

```text
source adapter
  -> main warehouse table
  -> warehouse.data_manifest materialization
  -> one parquet file per symbol
  -> manifest row update
```

### 5.3 Intraday live-update flow

```text
live_update_worker
  -> detect due 15m slots
  -> run_intraday_delta
  -> upsert intraday_ohlcv_15m_delta
  -> materialize per-symbol delta files
  -> emit structured logs
  -> ingest_observability_logs
  -> evaluate_alerts
```

### 5.4 EOD reconcile flow

```text
run_eod_reconcile
  -> refresh latest daily rows
  -> merge intraday delta into intraday base
  -> regenerate intraday base parquet files
  -> clear delta files / delta table rows
  -> update manifest
  -> bump refresh versions
  -> emit logs and alerts
```

### 5.5 Web read flow

```text
Browser
  -> NiceGUI page
  -> service layer
  -> main warehouse / observability DB
  -> TradingView Lightweight Charts / tables
```

The current web app is read-oriented. Manual control actions are exposed from the operations layer through a fixed action map, not arbitrary shell execution.

## 6. Current operational state

Snapshot from the current repo state on `2026-06-24`:

- web app is running in `tmux` session `bquant_web`
- main warehouse exists and is populated
- observability warehouse exists and is populated at a basic level
- current market session state at inspection time: `post_close`

Observed status snapshot:

- main plot datasets are stale relative to current date
- `intraday_ohlcv_15m_delta` is short-lived and may be empty outside live-update windows
- `dataset_refresh_state` exists but all tracked datasets are still at refresh version `0`
- operations page shows:
  - `failed_jobs_24h = 1`
  - `stale_symbol_count = 60`
- current alert set includes:
  - `daily_eod_missing`
  - `intraday_dataset_lag`
  - resolved `worker_heartbeat_lag`

## 7. What is already implemented vs. what is not done yet

### Implemented

- local web application with monitoring pages
- warehouse schema and plot-oriented views
- per-symbol Parquet materialization
- manifest tracking
- refresh-version table
- live-update worker and job entrypoints
- dbt project with DuckDB profile, source declarations, staging/intermediate models, marts, and data tests
- structured logging and observability projection
- alert evaluation engine
- resilience tests for `dry-run`, `no_data`, and worker one-shot scheduling

### Not fully proven yet

- true end-to-end live session ingestion with fresh market bars
- checkpoint advancement during real intraday updates
- refresh-version bumps from real successful delta/EOD runs
- sustained worker execution across multiple real trading slots
- replay or simulation mode for offline live-update validation
- wiring dbt runs into a scheduler or operations UI

## 8. Current constraints and known gaps

1. Local-only runtime:
   no Airflow, Prefect, systemd unit, or container orchestration is wired in yet.

2. No authentication layer:
   the operations UI assumes a trusted local environment.

3. Observability write locking:
   the design expects one ingest writer for the observability DB to avoid DuckDB lock conflicts.

4. Stale base data:
   the current base datasets stop at `2026-06-08`, while the repository date is `2026-06-24`.

5. Partial downstream stack:
   raw, silver, gold, and marts schemas exist, but current day-to-day usage is concentrated on the base datasets plus monitoring.

6. No historical replay mode yet:
   this is the main gap if the team wants to validate live-update behavior outside actual trading hours.
