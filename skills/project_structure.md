# Skill: Project Structure

## Purpose

Use this skill whenever creating files, folders, or deciding where code should live.

## Required Project Structure

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
│   ├── fetch_index_data.py
│   ├── fetch_vn30_universe.py
│   ├── fetch_vnquant_ohlcv.py
│   └── validate_ohlcv.py
│
├── warehouse/
│   ├── duckdb_connection.py
│   ├── init_duckdb.py
│   ├── parquet_io.py
│   ├── schema_duckdb.sql
│   └── views.sql
│
├── features/
│   ├── build_feature_table.py
│   ├── market_features.py
│   └── technical_features.py
│
├── backtesting/
│   ├── metrics.py
│   ├── reports.py
│   ├── run_backtest.py
│   └── strategies.py
│
├── models/
│   ├── build_dataset.py
│   ├── predict_signal.py
│   ├── train_baseline.py
│   └── train_lightgbm.py
│
├── nlp/
│   ├── clean_text.py
│   ├── collect_news.py
│   ├── entity_linking.py
│   ├── event_extraction.py
│   ├── rag_explanation.py
│   └── sentiment.py
│
├── api/
│   ├── main.py
│   └── routes/
│
├── dashboard/
│   └── streamlit_app.py
│
├── pipelines/
│   ├── daily_feature_pipeline.py
│   ├── daily_report_pipeline.py
│   ├── daily_signal_pipeline.py
│   └── daily_update_ohlcv.py
│
├── utils/
│   └── logger.py
│
├── airflow_dags/          # optional later
├── dbt_project/           # optional later
├── notebooks/
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

## Placement Rules

- Put source adapters and validators in `data_ingestion/`.
- Put DuckDB and Parquet access code in `warehouse/`.
- Put feature calculations in `features/`.
- Put simulation logic in `backtesting/`.
- Put training and prediction code in `models/`.
- Put news and LLM support in `nlp/`.
- Put dashboard code in `dashboard/`.
- Put orchestration entrypoints in `pipelines/`.
- Put utility code (logger, helpers) in `utils/`.
- Put tests in `tests/`.
- Put configuration files in `configs/`.
- Store raw data in `data/raw/`.
- Store cleaned data in `data/silver/`.
- Store feature data in `data/gold/`.
- Store consumption-ready data in `data/marts/`.
- Store DuckDB database in `warehouse/bquant.duckdb`.
- Store structured logs in `logs/data/` (JSONL format).
- Store pipeline logs in `logs/pipeline/`.
- Store error logs in `logs/errors/`.

## Avoid

- Do not mix ingestion, feature engineering, backtesting, and UI in one file.
- Do not put core business logic in notebooks.
- Do not put pipeline logic inside Streamlit or FastAPI handlers.
- Do not introduce ClickHouse-only paths into the MVP flow.
- Do not hard-code paths - always read from configs/storage.yaml.
- Do not overwrite raw data in data/raw/ without clear intent.
- Do not skip logging for data operations.
