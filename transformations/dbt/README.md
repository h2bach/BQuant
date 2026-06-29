# BQuant dbt Project

This dbt project turns BQuant's DuckDB warehouse tables into tested analytics and agent-ready marts.

## Commands

Run from the repository root:

```bash
conda run -n bquant dbt --project-dir transformations/dbt --profiles-dir transformations/dbt debug
conda run -n bquant dbt --project-dir transformations/dbt --profiles-dir transformations/dbt run
conda run -n bquant dbt --project-dir transformations/dbt --profiles-dir transformations/dbt test
```

## Layering

- `sources`: BQuant warehouse tables managed by Python ingestion and live-update jobs.
- `staging`: type-stable and naming-stable source wrappers.
- `intermediate`: reusable feature and quality transforms.
- `marts`: tables consumed by the web app, SQL Lab, and future Agentic AI workflows.

The ingestion layer remains Python-first. dbt owns transformations, tests, lineage, and mart contracts.
