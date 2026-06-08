# Skill: Pipeline Orchestration

## Purpose

Use this skill whenever wiring daily jobs, pipeline entrypoints, or later external schedulers.

## MVP Entry Points

Implement simple Python pipeline scripts first:

- `pipelines/daily_update_ohlcv.py`
- `pipelines/daily_feature_pipeline.py`
- `pipelines/daily_signal_pipeline.py`
- `pipelines/daily_report_pipeline.py`

## Daily Sequence

1. fetch OHLCV
2. validate and clean OHLCV
3. build technical features
4. build combined features
5. generate signals
6. optionally run backtest
7. refresh dashboard-ready outputs

## Later Integrations

Optional later files:

- `airflow_dags/quant_vn30_daily_dag.py`
- `prefect_flows/quant_vn30_daily_flow.py`
- `dbt_project/`

## Rules

- Keep core logic in project modules, not in DAG or flow definitions.
- DAG and flow tasks should call existing Python functions.
- Do not make Airflow, Prefect, or dbt a runtime dependency for the MVP pipeline.
- Keep daily scripts idempotent where practical.
