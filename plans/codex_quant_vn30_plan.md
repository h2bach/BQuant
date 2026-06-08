# Codex Plan — BQuant Trading System

> **Purpose**: This document is the master implementation plan for Codex when building BQuant Trading System.  
> **Current stage**: MVP / research system.  
> **Market**: Vietnam stock market.  
> **Initial universe**: VN30.  
> **Initial data source**: `vnquant` by phamdinhkhanh for historical OHLCV.  
> **Core storage decision**: Do **not** use PostgreSQL for the MVP. Use **DuckDB + Parquet Data Lake** as the local analytical lakehouse. Use **ClickHouse** later for near-real-time/intraday analytics.

---

## 1. System Goals

Build a modular quantitative trading research platform that supports:

1. Historical OHLCV ingestion for VN30.
2. Data lakehouse organization using Raw/Silver/Gold/Marts layers.
3. Technical feature engineering.
4. Baseline strategy backtesting.
5. Rule-based signal generation.
6. ML-based modeling after the baseline is stable.
7. NLP features from news/reports after the OHLCV pipeline works.
8. LLM-based explanation layer using retrieved structured data.
9. Dashboard for reviewing signals and backtest results.
10. Future extension to near-real-time data and paper trading.
11. Comprehensive logging for all data operations.

This system is for **research and decision support**, not direct investment advice or fully automated live trading in the MVP phase.

---

## 2. Core Design Decisions

### 2.1. Use DuckDB + Parquet Data Lake

The project should not use PostgreSQL as the primary MVP warehouse.

Use:

```text
DuckDB database file: warehouse/bquant.duckdb
Parquet data lake:    data/raw/, data/silver/, data/gold/, data/marts/
```

Rationale:

- DuckDB is lightweight and embedded.
- It is well suited for local analytical workflows.
- It works directly with Parquet.
- It avoids maintaining a database server during MVP.
- It is suitable for OHLCV, feature tables, backtest result tables, and local analytics.
- Parquet provides durable, columnar storage for large datasets.
- Data lake architecture separates raw, silver, gold, and marts layers.

### 2.2. Use ClickHouse later for realtime/intraday analytics

When the system grows to near-real-time VN30/VNIndex feeds, use ClickHouse as an optional production analytics backend.

Use ClickHouse for:

- intraday bars
- ticks or quote snapshots
- high-frequency append-only market data
- dashboard queries over larger data volumes
- long-term production analytics

Do not introduce ClickHouse in the first MVP unless necessary.

### 2.3. Use Parquet as the durable data format

All key datasets should also be saved as Parquet.

Recommended layout:

```text
data/
├── raw/                    # Raw or near-raw collected data
│   └── ohlcv/
│       └── source=vnquant/
│           └── symbol=FPT/*.parquet
├── silver/                 # Cleaned and standardized datasets
│   ├── clean_ohlcv_daily/*.parquet
│   └── universe_members/*.parquet
├── gold/                   # Feature datasets
│   ├── technical_features_daily/*.parquet
│   └── combined_features_daily/*.parquet
└── marts/                  # Consumption-ready datasets
    ├── trading_signals/*.parquet
    └── backtest_results/*.parquet
```

DuckDB can query these Parquet datasets directly.

---

## 3. Architecture Overview

```text
Data Sources
  ├── vnquant historical OHLCV
  ├── VN30 universe config
  ├── VNIndex / VN30 benchmark data
  ├── News and reports, later phase
  └── Realtime feeds, later phase
        ↓
Data Ingestion Layer (with logging)
        ↓
Raw Layer: raw Parquet (data/raw/)
        ↓
Silver Layer: cleaned OHLCV, cleaned universe (data/silver/)
        ↓
Gold Layer: technical features, combined features (data/gold/)
        ↓
Marts Layer: trading signals, backtest results (data/marts/)
        ↓
DuckDB: warehouse/bquant.duckdb for querying and analysis
        ↓
Analytics and Modeling
  ├── Technical score
  ├── ML model
  ├── NLP score
  └── LLM explanation
        ↓
Backtesting and Risk
        ↓
Dashboard / API / Reports
        ↓
Paper trading, later phase
```

---

## 4. Technology Stack

### 4.1. MVP stack

| Component | Tool | Codex responsibility |
|---|---|---|
| Environment | Conda | Create `environment.yml`, setup instructions |
| Historical OHLCV | `vnquant` | Implement adapter and standardization |
| Local warehouse | DuckDB | Implement Python bindings and SQL scripts |
| Durable data | Parquet | Implement read/write utilities |
| Data processing | Pandas / Polars | Implement transformations |
| Technical indicators | `ta`, `pandas-ta`, custom functions | Implement feature builders |
| Backtesting | vectorbt or custom engine | Implement baseline backtest |
| ML | scikit-learn, LightGBM, XGBoost | Implement training scripts later |
| Dashboard | Streamlit | Implement Streamlit app code only |
| API | FastAPI | Implement API bindings later |
| Notebook testing | JupyterLab | Use only for exploration, not core logic |

### 4.2. Later production stack

| Component | Tool | Codex responsibility |
|---|---|---|
| Orchestration | Airflow / Prefect | Implement DAG/flow files and task bindings only |
| Transform layer | dbt | Create dbt models/config only; do not replace dbt runtime |
| Realtime analytics | ClickHouse | Implement ClickHouse schema and Python client bindings |
| BI dashboard | Tableau / Superset / Grafana | Prepare data views/API/extracts; do not create proprietary app dashboards in code |
| Vector database | Qdrant / Chroma / pgvector alternative | Implement ingestion/query wrappers |
| LLM | OpenAI API or local LLM | Implement prompt/RAG bindings only |
| Model tracking | MLflow | Implement logging calls and config only |

---

## 5. External Applications and What Codex Should Do

Some components are applications or services, not pure code libraries. Codex should not try to “re-implement” them. Codex should create only bindings, configs, wrappers, and integration code.

| External component | What it is | Codex should implement | Codex should not implement |
|---|---|---|---|
| DuckDB | Embedded analytical DB | Python connection utilities, SQL files, table/view creation | N/A, DuckDB is library-like and can be used directly |
| ClickHouse | External OLAP database/server | DDL, Python client wrapper, insert/query functions | Do not manage cluster infrastructure unless requested |
| dbt | External transformation framework | `dbt_project.yml`, SQL models, profiles template, model docs | Do not execute dbt in normal app code |
| Airflow | External orchestration platform | DAG files and task wrappers | Do not build a custom scheduler to replace Airflow |
| Prefect | External orchestration platform | Flow/task files | Do not duplicate all pipeline logic inside Prefect flows |
| Streamlit | External dashboard runtime | `dashboard/streamlit_app.py` | Do not put core pipeline logic inside Streamlit |
| Tableau | External BI tool | Export tables/views/CSV/Parquet/extract-ready datasets | Do not generate Tableau workbooks unless explicitly requested |
| Superset/Grafana | External BI tools | SQL views, API endpoints, datasource config notes | Do not hard-code dashboards unless asked |
| MLflow | External tracking tool | MLflow logging functions and model registry hooks | Do not make MLflow required for MVP |
| Qdrant/Chroma | External/local vector DB | Document indexing/query wrappers | Do not mix vector data into OHLCV warehouse tables |
| LLM provider | External model/API | Prompt templates, client wrapper, structured output parser | Do not let LLM trade or invent market data |

---

## 6. Project Structure

Codex should create and maintain this structure:

```text
bquant/
│
├── configs/
│   ├── bquant.yaml
│   ├── storage.yaml
│   ├── data_sources.yaml
│   ├── universe_vn30.yaml
│   ├── feature_config.yaml
│   ├── strategy_config.yaml
│   ├── dataset_registry.yaml
│   └── validation_rules.yaml
│
├── data_ingestion/
│   ├── fetch_vn30_universe.py
│   ├── fetch_vnquant_ohlcv.py
│   ├── fetch_index_data.py
│   └── validate_ohlcv.py
│
├── warehouse/
│   ├── duckdb_connection.py
│   ├── schema_duckdb.sql
│   ├── init_duckdb.py
│   ├── parquet_io.py
│   └── views.sql
│
├── features/
│   ├── technical_features.py
│   ├── market_features.py
│   └── build_feature_table.py
│
├── backtesting/
│   ├── strategies.py
│   ├── run_backtest.py
│   ├── metrics.py
│   └── reports.py
│
├── models/
│   ├── build_dataset.py
│   ├── train_baseline.py
│   ├── train_lightgbm.py
│   └── predict_signal.py
│
├── nlp/
│   ├── collect_news.py
│   ├── clean_text.py
│   ├── entity_linking.py
│   ├── sentiment.py
│   ├── event_extraction.py
│   └── rag_explanation.py
│
├── api/
│   ├── main.py
│   └── routes/
│
├── dashboard/
│   └── streamlit_app.py
│
├── pipelines/
│   ├── daily_update_ohlcv.py
│   ├── daily_feature_pipeline.py
│   ├── daily_signal_pipeline.py
│   └── daily_report_pipeline.py
│
├── utils/
│   └── logger.py
│
├── dbt_project/                 # Optional later
│   ├── dbt_project.yml
│   ├── models/
│   └── README.md
│
├── airflow_dags/                # Optional later
│   └── bquant_daily_dag.py
│
├── notebooks/
│   └── test_vnquant.ipynb
│
├── tests/
├── logs/
│   ├── data/
│   │   ├── ingestion/
│   │   ├── features/
│   │   ├── backtesting/
│   │   └── signals/
│   ├── pipeline/
│   └── errors/
├── data/
│   ├── raw/
│   │   └── ohlcv/
│   │       └── source=vnquant/
│   ├── silver/
│   │   ├── clean_ohlcv_daily/
│   │   └── universe_members/
│   ├── gold/
│   │   ├── technical_features_daily/
│   │   └── combined_features_daily/
│   └── marts/
│       ├── trading_signals/
│       └── backtest_results/
├── warehouse/
│   └── bquant.duckdb
│
├── .env
├── .env.example
├── .gitignore
├── environment.yml
├── requirements.txt
└── README.md
```

---

## 7. Conda Environment

Use Conda for environment control.

### 7.1. Environment name

```text
bquant
```

### 7.2. Python version

```text
Python 3.10
```

### 7.3. Recommended `environment.yml`

```yaml
name: bquant

channels:
  - conda-forge
  - defaults

dependencies:
  - python=3.10
  - pandas
  - numpy
  - polars
  - duckdb
  - pyarrow
  - sqlalchemy
  - python-dotenv
  - pyyaml
  - jupyterlab
  - matplotlib
  - plotly
  - streamlit
  - scikit-learn
  - lightgbm
  - xgboost
  - fastapi
  - uvicorn
  - pip
  - pip:
      - vnquant
      - vnstock
      - ta
      - pandas-ta
      - vectorbt
      - pytest
```

### 7.4. Setup commands

```bash
conda env create -f environment.yml
conda activate bquant
```

or:

```bash
conda create -n bquant python=3.10 -y
conda activate bquant
conda install -c conda-forge pandas numpy polars duckdb pyarrow python-dotenv pyyaml -y
conda install -c conda-forge jupyterlab matplotlib plotly streamlit -y
conda install -c conda-forge scikit-learn lightgbm xgboost fastapi uvicorn -y
pip install vnquant vnstock ta pandas-ta vectorbt pytest
```

---

## 8. Data Lakehouse Design

### 8.1. Raw layer

Raw data exactly or almost exactly as collected.

Examples:

```text
raw_ohlcv_vnquant
raw_news_raw
raw_index_data
```

Parquet paths:

```text
data/raw/ohlcv/source=vnquant/symbol=<SYMBOL>/part-*.parquet
data/raw/news/source=<SOURCE>/part-*.parquet
```

### 8.2. Silver layer

Cleaned, standardized data.

Examples:

```text
silver_clean_ohlcv_daily
silver_universe_members
silver_clean_news
```

Parquet paths:

```text
data/silver/clean_ohlcv_daily/*.parquet
data/silver/universe_members/*.parquet
```

### 8.3. Gold layer

Feature/model/signal-ready data.

Examples:

```text
gold_technical_features_daily
gold_combined_features_daily
```

Parquet paths:

```text
data/gold/technical_features_daily/*.parquet
data/gold/combined_features_daily/*.parquet
```

### 8.4. Marts layer

Consumption-ready data for dashboard, backtest, signals.

Examples:

```text
marts_trading_signals
marts_backtest_runs
```

Parquet paths:

```text
data/marts/trading_signals/*.parquet
data/marts/backtest_results/*.parquet
```

---

## 9. DuckDB Schema

Codex should create `warehouse/schema_duckdb.sql`.

Use DuckDB tables or views over Parquet. For MVP, physical DuckDB tables are acceptable. Also save durable copies to Parquet.

### 9.1. Universe table

```sql
CREATE TABLE IF NOT EXISTS universe_members (
    universe_name VARCHAR,
    symbol VARCHAR,
    effective_date DATE,
    end_date DATE,
    source VARCHAR,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (universe_name, symbol, effective_date)
);
```

### 9.2. Raw OHLCV table

```sql
CREATE TABLE IF NOT EXISTS raw_ohlcv (
    symbol VARCHAR,
    trading_date DATE,
    open DOUBLE,
    high DOUBLE,
    low DOUBLE,
    close DOUBLE,
    adjusted_close DOUBLE,
    volume DOUBLE,
    source VARCHAR,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

### 9.3. Clean OHLCV table

```sql
CREATE TABLE IF NOT EXISTS clean_ohlcv_daily (
    symbol VARCHAR,
    trading_date DATE,
    open DOUBLE,
    high DOUBLE,
    low DOUBLE,
    close DOUBLE,
    adjusted_close DOUBLE,
    volume DOUBLE,
    source VARCHAR,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (symbol, trading_date)
);
```

### 9.4. Technical features table

```sql
CREATE TABLE IF NOT EXISTS technical_features_daily (
    symbol VARCHAR,
    trading_date DATE,
    return_1d DOUBLE,
    return_5d DOUBLE,
    return_20d DOUBLE,
    log_return_1d DOUBLE,
    volatility_20d DOUBLE,
    ma_20 DOUBLE,
    ma_50 DOUBLE,
    price_to_ma20 DOUBLE,
    rsi_14 DOUBLE,
    macd DOUBLE,
    macd_signal DOUBLE,
    atr_14 DOUBLE,
    volume_zscore_20 DOUBLE,
    trend_score DOUBLE,
    momentum_score DOUBLE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (symbol, trading_date)
);
```

### 9.5. Combined features table

```sql
CREATE TABLE IF NOT EXISTS combined_features_daily (
    symbol VARCHAR,
    trading_date DATE,
    return_1d DOUBLE,
    return_5d DOUBLE,
    return_20d DOUBLE,
    volatility_20d DOUBLE,
    rsi_14 DOUBLE,
    macd DOUBLE,
    volume_zscore_20 DOUBLE,
    technical_score DOUBLE,
    nlp_score DOUBLE,
    market_regime_score DOUBLE,
    risk_score DOUBLE,
    target_return_5d DOUBLE,
    target_up_5d INTEGER,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (symbol, trading_date)
);
```

### 9.6. Trading signals table

```sql
CREATE TABLE IF NOT EXISTS trading_signals (
    signal_id UUID DEFAULT uuid(),
    symbol VARCHAR,
    signal_date DATE,
    technical_score DOUBLE,
    nlp_score DOUBLE,
    market_regime_score DOUBLE,
    risk_score DOUBLE,
    final_score DOUBLE,
    signal VARCHAR,
    model_version VARCHAR,
    explanation VARCHAR,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (signal_id)
);
```

### 9.7. Backtest runs table

```sql
CREATE TABLE IF NOT EXISTS backtest_runs (
    run_id UUID DEFAULT uuid(),
    strategy_name VARCHAR,
    universe VARCHAR,
    start_date DATE,
    end_date DATE,
    total_return DOUBLE,
    annualized_return DOUBLE,
    volatility DOUBLE,
    sharpe DOUBLE,
    max_drawdown DOUBLE,
    win_rate DOUBLE,
    turnover DOUBLE,
    transaction_cost DOUBLE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (run_id)
);
```

---

## 10. DuckDB Binding Utilities

Codex should implement `warehouse/duckdb_connection.py`.

Expected functions:

```python
def get_duckdb_path() -> str:
    ...

def get_connection(read_only: bool = False):
    ...

def execute_sql_file(sql_path: str) -> None:
    ...

def query_df(sql: str):
    ...

def write_df(table_name: str, df, mode: str = "append") -> None:
    ...
```

Codex should implement `warehouse/parquet_io.py`.

Expected functions:

```python
def write_parquet(df, path: str, partition_cols: list[str] | None = None) -> None:
    ...

def read_parquet(path: str):
    ...

def export_table_to_parquet(table_name: str, output_path: str) -> None:
    ...
```

---

## 11. Data Ingestion Plan

### 11.1. VN30 universe

Codex should create `configs/universe_vn30.yaml`.

Example:

```yaml
universe_name: VN30
effective_date: "2026-01-01"
symbols:
  - ACB
  - BCM
  - BID
  - BVH
  - CTG
  - FPT
  - GAS
  - GVR
  - HDB
  - HPG
  - MBB
  - MSN
  - MWG
  - PLX
  - POW
  - SAB
  - SHB
  - SSB
  - SSI
  - STB
  - TCB
  - TPB
  - VCB
  - VHM
  - VIB
  - VIC
  - VJC
  - VNM
  - VPB
  - VRE
```

Codex should implement:

```text
data_ingestion/fetch_vn30_universe.py
```

Function requirements:

```python
def load_vn30_universe(config_path: str) -> pandas.DataFrame:
    ...

def save_universe_to_duckdb(df) -> None:
    ...
```

### 11.2. OHLCV ingestion with vnquant

Codex should implement:

```text
data_ingestion/fetch_vnquant_ohlcv.py
```

The adapter must standardize output to:

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

Expected functions:

```python
def get_vn30_symbols() -> list[str]:
    ...

def fetch_ohlcv_vnquant(symbol: str, start_date: str, end_date: str):
    ...

def standardize_ohlcv(df, symbol: str, source: str = "vnquant"):
    ...

def save_raw_ohlcv(df) -> None:
    ...

def main():
    ...
```

If the exact `vnquant` API is uncertain, Codex should write adapter code with isolated API-specific logic and include a notebook or script for testing one symbol, e.g. `FPT`.

### 11.3. OHLCV validation

Codex should implement:

```text
data_ingestion/validate_ohlcv.py
```

Validation rules:

- no missing symbol
- valid trading_date
- numeric open, high, low, close, volume
- high >= low
- volume >= 0
- remove duplicates by symbol + trading_date
- sort by symbol + trading_date

Output:

```text
clean_ohlcv_daily
```

Also export:

```text
data/lake/silver/clean_ohlcv_daily/*.parquet
```

---

## 12. Feature Engineering Plan

Codex should implement:

```text
features/technical_features.py
features/build_feature_table.py
```

### 12.1. Technical features

Compute per symbol:

```text
return_1d
return_5d
return_20d
log_return_1d
volatility_20d
ma_20
ma_50
price_to_ma20
rsi_14
macd
macd_signal
atr_14
volume_zscore_20
trend_score
momentum_score
```

Rules:

- Always sort by symbol and trading_date.
- Rolling features must be computed per symbol.
- Do not compute rolling statistics across symbols.
- Drop early rows with insufficient history only in derived feature tables.

### 12.2. Combined features and labels

Create:

```text
technical_score
nlp_score
market_regime_score
risk_score
target_return_5d
target_up_5d
```

For MVP:

```text
nlp_score = 0.0
market_regime_score = 0.5
risk_score = volatility rank
```

Target definition:

```text
target_return_5d = adjusted_close[t+5] / adjusted_close[t] - 1
target_up_5d = 1 if target_return_5d > 0 else 0
```

Important:

- Targets are for training/evaluation only.
- Do not use targets to generate live signals.

---

## 13. Backtesting Plan

Codex should implement:

```text
backtesting/run_backtest.py
backtesting/strategies.py
backtesting/metrics.py
```

### 13.1. MVP strategy

Top-K technical score strategy:

```text
1. On rebalance date, rank VN30 symbols by technical_score.
2. Select top K symbols.
3. Allocate equal weight.
4. Hold until next rebalance date.
5. Apply transaction cost.
6. Save metrics to backtest_runs.
```

Default parameters:

```text
top_k = 5
rebalance_freq = weekly
transaction_cost = 0.0015
```

### 13.2. Metrics

Compute:

```text
total_return
annualized_return
volatility
sharpe
max_drawdown
win_rate
turnover
transaction_cost
```

Critical rule:

```text
Use shifted weights when computing portfolio return.
```

This avoids same-day execution bias.

---

## 14. Signal Generation Plan

Codex should implement:

```text
models/predict_signal.py
```

Baseline formula:

```text
final_score =
    0.45 * technical_score
  + 0.25 * nlp_score
  + 0.15 * market_regime_score
  - 0.15 * risk_score
```

Signal labels:

```text
BUY   = top 20% final_score
AVOID = bottom 20% final_score
HOLD  = otherwise
```

Output table:

```text
trading_signals
```

Also export:

```text
data/lake/gold/trading_signals/*.parquet
```

---

## 15. Dashboard Plan

Dashboard runtime: **Streamlit**.

Codex should implement only:

```text
dashboard/streamlit_app.py
```

Codex should not move business logic into the dashboard.

Dashboard should read from DuckDB and show:

1. Latest trading signals.
2. Top BUY candidates.
3. AVOID/RISK candidates.
4. Backtest result table.
5. Feature summary by symbol.
6. Later: LLM explanations.

Run command:

```bash
streamlit run dashboard/streamlit_app.py
```

---

## 16. Pipeline Orchestration Plan

### 16.1. MVP pipeline scripts

Codex should implement simple Python pipelines first:

```text
pipelines/daily_update_ohlcv.py
pipelines/daily_feature_pipeline.py
pipelines/daily_signal_pipeline.py
pipelines/daily_report_pipeline.py
```

Main daily sequence:

```text
1. Fetch OHLCV
2. Validate OHLCV
3. Build technical features
4. Build combined features
5. Generate signals
6. Run optional backtest
7. Refresh dashboard-ready data
```

### 16.2. Airflow or Prefect later

Airflow/Prefect are external orchestration applications.

Codex should only create:

```text
airflow_dags/quant_vn30_daily_dag.py
```

or:

```text
prefect_flows/quant_vn30_daily_flow.py
```

Do not duplicate pipeline logic inside DAG/flow files. DAG tasks should call functions from project modules.

---

## 17. dbt Plan

`dbt` is optional and later-stage.

If dbt is introduced, Codex should create:

```text
dbt_project/
├── dbt_project.yml
├── models/
│   ├── bronze/
│   ├── silver/
│   └── gold/
└── README.md
```

Codex should create SQL models only. It should not replace Python feature engineering where Python is more natural, especially for rolling indicators and ML-prep logic.

Potential dbt responsibility:

- clean raw OHLCV
- create model-ready views
- validate row counts
- create dashboard views

Codex should not rely on dbt for the initial MVP.

---

## 18. NLP and LLM Extension Plan

This is not required for the first OHLCV MVP.

### 18.1. NLP pipeline

Later pipeline:

```text
news source
→ raw_news
→ clean_text
→ entity_linking
→ sentiment_analysis
→ event_extraction
→ nlp_features_daily
→ combined_features_daily
```

Initial NLP features:

```text
sentiment_1d
sentiment_3d
sentiment_7d
positive_event_count
negative_event_count
risk_event_score
news_count
```

### 18.2. LLM explanation

LLM should explain signals, not directly produce trading decisions.

Codex should implement:

```text
nlp/rag_explanation.py
```

Expected structured output:

```json
{
  "symbol": "FPT",
  "signal": "BUY",
  "summary": "...",
  "positive_factors": [],
  "negative_factors": [],
  "risk_factors": [],
  "confidence": 0.0,
  "disclaimer": "This is not financial advice."
}
```

Rules:

- LLM must not invent market data.
- LLM must use retrieved context from warehouse/vector store.
- LLM explanations should be stored in `trading_signals.explanation` or a separate explanation table later.

---

## 19. Optional ClickHouse Plan for Realtime

ClickHouse should be added only when the project needs near-real-time or intraday analytics.

Codex should create optional files:

```text
warehouse/clickhouse_schema.sql
warehouse/clickhouse_client.py
pipelines/realtime_ingestion_pipeline.py
```

Suggested tables:

```sql
CREATE TABLE IF NOT EXISTS intraday_bars (
    symbol String,
    timestamp DateTime,
    timeframe String,
    open Float64,
    high Float64,
    low Float64,
    close Float64,
    volume Float64,
    source String,
    created_at DateTime DEFAULT now()
)
ENGINE = MergeTree
ORDER BY (symbol, timeframe, timestamp);
```

```sql
CREATE TABLE IF NOT EXISTS market_snapshots (
    symbol String,
    timestamp DateTime,
    last_price Float64,
    change_percent Float64,
    bid_price_1 Float64,
    bid_volume_1 Float64,
    ask_price_1 Float64,
    ask_volume_1 Float64,
    total_volume Float64,
    source String,
    created_at DateTime DEFAULT now()
)
ENGINE = MergeTree
ORDER BY (symbol, timestamp);
```

MVP should not depend on ClickHouse.

---

## 20. Testing and Quality Rules

Codex should create tests under:

```text
tests/
```

### 20.1. Data tests

Check:

- no duplicate symbol + trading_date in clean OHLCV
- high >= low
- volume >= 0
- close > 0
- no missing symbol
- date is valid

### 20.2. Feature tests

Check:

- rolling features are computed per symbol
- labels use correct future shift
- feature table does not include target leakage in signal columns
- technical_score is within expected range

### 20.3. Backtest tests

Check:

- weights sum <= 1
- transaction costs applied
- returns use shifted weights
- no target_return used in strategy decision

### 20.4. Model tests

Check:

- time-based train/test split
- no target columns in feature columns
- predictions contain symbol/date/model_version

---

## 21. Codex Implementation Order

Codex should implement in this order:

```text
1. Storage architecture: data/raw, data/silver, data/gold, data/marts, warehouse/
2. Config files: bquant.yaml, storage.yaml, data_sources.yaml, universe_vn30.yaml
3. DuckDB schema and initialization (warehouse/bquant.duckdb)
4. Dataset registry (configs/dataset_registry.yaml)
5. Validation rules (configs/validation_rules.yaml)
6. Logging utilities (utils/logger.py)
7. DuckDB connection utilities
8. VN30 universe config and loader (test: FPT, HPG, VCB)
9. vnquant OHLCV adapter
10. OHLCV validation and cleaning
11. Technical feature builder
12. Combined feature table builder
13. Baseline backtest
14. Signal generator
15. Streamlit dashboard
16. Tests
17. ML model
18. NLP and LLM extension
19. Airflow/Prefect/dbt bindings
20. ClickHouse realtime extension
```

Do not start with ML, NLP, LLM, or realtime before the OHLCV + backtest MVP works.
Follow the implementation order: storage architecture → schema → data pipelines.

---

## 22. Commands Codex Should Document in README

```bash
conda env create -f environment.yml
conda activate bquant

python -m warehouse.init_duckdb
python -m data_ingestion.fetch_vn30_universe
python -m data_ingestion.fetch_vnquant_ohlcv
python -m data_ingestion.validate_ohlcv
python -m features.technical_features
python -m features.build_feature_table
python -m backtesting.run_backtest
python -m models.predict_signal
streamlit run dashboard/streamlit_app.py
```

---

## 23. Safety and Scope Constraints

Codex must follow these constraints:

1. Do not implement real-money live trading in MVP.
2. Do not store API keys in code.
3. Do not hard-code credentials.
4. Do not let LLM invent OHLCV, metrics, or news.
5. Do not use target labels for signal generation.
6. Do not random-shuffle time-series data for model validation.
7. Do not ignore transaction costs in backtesting.
8. Do not put pipeline logic inside dashboard code.
9. Do not depend on external applications such as dbt/Airflow/Tableau for MVP.
10. Do not overwrite raw data without clear intent.
11. All dataset paths must come from configs/dataset_registry.yaml, never hard-coded.
12. All data operations must be logged to logs/data/ with structured JSONL format.
13. Follow the implementation order: storage architecture → schema → data pipelines.

---

## 24. MVP Definition of Done

The MVP is complete when:

1. Conda environment can be created from `environment.yml`.
2. Storage architecture is set up: data/raw, data/silver, data/gold, data/marts, warehouse/
3. Config files are created: bquant.yaml, storage.yaml, data_sources.yaml, universe_vn30.yaml
4. DuckDB database (warehouse/bquant.duckdb) initializes successfully.
5. Dataset registry (configs/dataset_registry.yaml) is created.
6. Validation rules (configs/validation_rules.yaml) are defined.
7. Logging utilities (utils/logger.py) are implemented.
8. VN30 universe is loaded (test: FPT, HPG, VCB).
9. OHLCV for test symbols can be ingested from `vnquant`.
10. Clean OHLCV is saved to DuckDB and Parquet (data/silver/).
11. Technical features are computed and saved (data/gold/).
12. Combined features and labels are created and saved (data/gold/).
13. Baseline Top-K backtest runs.
14. Backtest metrics are saved (data/marts/).
15. Daily trading signals are generated and saved (data/marts/).
16. All data operations are logged to logs/data/ with JSONL format.
17. Streamlit dashboard displays signals and backtest results.
18. Basic tests pass.

---

## 25. Future Roadmap

After MVP:

```text
Phase 2: ML models
  - LightGBM/XGBoost
  - time-based validation
  - model comparison

Phase 3: NLP
  - news collection
  - entity linking
  - sentiment
  - event extraction

Phase 4: LLM explanation
  - structured signal explanation
  - RAG over news and feature summaries

Phase 5: Orchestration
  - Prefect or Airflow
  - scheduled daily pipeline

Phase 6: Realtime analytics
  - ClickHouse
  - near-real-time VN30/VNIndex feed

Phase 7: Paper trading
  - simulated portfolio
  - signal vs realized return tracking

Phase 8: Controlled live integration
  - only if explicitly requested
  - broker API
  - manual approval
  - risk kill switch
```

---

## 26. Final Instruction to Codex

Build the simplest correct system first.

Use:

```text
DuckDB + Parquet Data Lake for MVP.
  - Parquet: durable storage in data/raw, data/silver, data/gold, data/marts
  - DuckDB: analytical engine at warehouse/bquant.duckdb
ClickHouse only for future realtime/intraday.
Streamlit for dashboard code.
dbt/Airflow/Tableau only as external tools with integration bindings.
All dataset paths from configs/dataset_registry.yaml.
All data operations logged to logs/data/ with JSONL format.
```

The first implementation target is:

```text
BQuant + VN30 + vnquant OHLCV + DuckDB + Parquet Data Lake + technical features + baseline backtest + signal dashboard
```

Implementation order: storage architecture → schema → config → validation → logging → data pipelines.

Do not expand into NLP/LLM/realtime until this core pipeline works.
