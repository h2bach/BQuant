# Skill: Environment Setup

## Purpose

Use this skill whenever creating or updating the local development environment for BQuant.

## Baseline Environment

- Environment name: `quant-vn30`
- Python version: `3.10`
- Manager: `Conda`

## Recommended Dependencies

Core packages:

- `pandas`
- `numpy`
- `polars`
- `duckdb`
- `pyarrow`
- `sqlalchemy`
- `python-dotenv`
- `pyyaml`

Research and visualization:

- `jupyterlab`
- `matplotlib`
- `plotly`
- `streamlit`

Modeling and service:

- `scikit-learn`
- `lightgbm`
- `xgboost`
- `fastapi`
- `uvicorn`

Pip-only or likely pip-installed packages:

- `vnquant`
- `vnstock`
- `ta`
- `pandas-ta`
- `vectorbt`
- `pytest`

## Standard Commands

```bash
conda env create -f environment.yml
conda activate quant-vn30
```

Fallback manual setup:

```bash
conda create -n quant-vn30 python=3.10 -y
conda activate quant-vn30
```

## Rules

- Prefer `environment.yml` as the source of truth.
- Keep notebook-only tools optional; do not move core logic into notebooks.
- Keep source-specific packages isolated behind adapters.
- Do not make ClickHouse, dbt, or Airflow required for MVP setup.
