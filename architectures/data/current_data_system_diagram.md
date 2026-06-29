# BQuant Current Data System Diagram

Snapshot date: `2026-06-29`

This document visualizes the current BQuant data platform as implemented in the repository. It focuses on market data, live update state, dbt analytics marts, observability, and the future news/vector extension point.

Naming note: `VCI-data-source` is the human-facing provider label for the `vnstock` API source code `VCI`. It does not mean the stock symbol `VCI`; stock symbols are passed separately as values such as `VCB`, `FPT`, `VNINDEX`, or `VN30`.

## 1. End-to-End Data Flow

```mermaid
flowchart LR
    subgraph Sources["External Sources"]
        VCI["vnstock<br/>VCI-data-source provider<br/>VN30 stocks, VNINDEX, VN30"]
        FutureNews["Future news/social sources<br/>news, forums, social posts"]
    end

    subgraph Ingestion["Python Ingestion + Pipeline Jobs"]
        DailyFetch["fetch_daily_10y_base"]
        IndexFetch["fetch_market_index_daily_10y"]
        IntraFetch["fetch_intraday_15m"]
        DeltaJob["run_intraday_delta"]
        EODJob["run_eod_reconcile"]
        ManifestJob["refresh_manifest"]
    end

    subgraph MainDuckDB["warehouse/bquant.duckdb"]
        Universe["universe_members"]
        DailyBase["daily_ohlcv_base"]
        IndexBase["market_index_daily_base"]
        IntraBase["intraday_ohlcv_15m_base"]
        IntraDelta["intraday_ohlcv_15m_delta"]
        Manifest["data_file_manifest"]
        RefreshState["dataset_refresh_state"]
        PipelineRuns["pipeline_runs"]
    end

    subgraph ParquetLake["Parquet Lake"]
        DailyFiles["data/base/daily_10y<br/>one file per symbol"]
        IndexFiles["data/base/market_index_daily_10y<br/>one file per index"]
        IntraFiles["data/base/intraday_15m_60d<br/>one file per symbol"]
        DeltaFiles["data/base/intraday_15m_delta<br/>short-lived delta files"]
        ManifestFile["data/metadata/data_file_manifest.parquet"]
    end

    subgraph Dbt["dbt on DuckDB"]
        Staging["analytics_staging<br/>source wrappers"]
        Intermediate["analytics_intermediate<br/>returns, breadth, liquidity, freshness"]
        Marts["analytics_marts<br/>agent-ready serving marts"]
    end

    subgraph Consumers["Consumers"]
        Web["NiceGUI Web App<br/>charts, SQL Lab, operations"]
        Notebooks["Data integrity notebooks"]
        FutureAgents["Future Agentic AI layer"]
    end

    VCI --> DailyFetch --> DailyBase
    VCI --> IndexFetch --> IndexBase
    VCI --> IntraFetch --> IntraBase
    VCI --> DeltaJob --> IntraDelta
    IntraDelta --> EODJob --> IntraBase
    EODJob --> DailyBase

    DailyBase --> ManifestJob
    IndexBase --> ManifestJob
    IntraBase --> ManifestJob
    IntraDelta --> ManifestJob

    ManifestJob --> Manifest
    ManifestJob --> DailyFiles
    ManifestJob --> IndexFiles
    ManifestJob --> IntraFiles
    ManifestJob --> DeltaFiles
    Manifest --> ManifestFile

    DailyBase --> Staging
    IndexBase --> Staging
    IntraBase --> Staging
    IntraDelta --> Staging
    Universe --> Staging
    Manifest --> Staging
    Staging --> Intermediate --> Marts

    DailyBase --> Web
    IndexBase --> Web
    IntraBase --> Web
    IntraDelta --> Web
    RefreshState --> Web
    Marts --> Web
    Marts --> FutureAgents
    Marts --> Notebooks

    FutureNews -. future phase .-> FutureAgents
```

## 2. DuckDB Schemas And Responsibilities

```mermaid
flowchart TB
    subgraph MainSchema["main schema"]
        BaseTables["Base market tables<br/>daily_ohlcv_base<br/>market_index_daily_base<br/>intraday_ohlcv_15m_base<br/>intraday_ohlcv_15m_delta"]
        ControlTables["Control tables<br/>data_file_manifest<br/>dataset_refresh_state<br/>pipeline_runs"]
        UniverseTables["Universe tables<br/>universe_members"]
        LegacyFeatureTables["Legacy/planned feature tables<br/>technical_features_daily<br/>combined_features_daily<br/>trading_signals<br/>backtest_runs"]
    end

    subgraph AnalyticsStaging["analytics_staging"]
        StgDaily["stg_daily_ohlcv"]
        StgIndex["stg_market_index_daily"]
        StgIntra["stg_intraday_15m"]
        StgUniverse["stg_universe_members"]
        StgManifest["stg_data_file_manifest"]
    end

    subgraph AnalyticsIntermediate["analytics_intermediate"]
        IntReturns["int_symbol_daily_returns"]
        IntLiquidity["int_symbol_liquidity_daily"]
        IntIndex["int_market_index_returns"]
        IntBreadth["int_market_breadth_daily"]
        IntIntra["int_intraday_daily_summary"]
        IntFreshness["int_data_freshness_status"]
    end

    subgraph AnalyticsMarts["analytics_marts"]
        MartRegime["mart_market_regime_daily"]
        MartFeatures["mart_symbol_daily_features"]
        MartQuality["mart_symbol_data_quality"]
        MartAgent["mart_agent_context_daily"]
    end

    BaseTables --> StgDaily
    BaseTables --> StgIndex
    BaseTables --> StgIntra
    UniverseTables --> StgUniverse
    ControlTables --> StgManifest

    StgDaily --> IntReturns
    StgDaily --> IntLiquidity
    StgDaily --> IntBreadth
    StgIndex --> IntIndex
    StgIntra --> IntIntra
    StgManifest --> IntFreshness

    IntIndex --> MartRegime
    IntBreadth --> MartRegime
    IntReturns --> MartFeatures
    IntLiquidity --> MartFeatures
    IntIndex --> MartFeatures
    IntFreshness --> MartQuality
    MartRegime --> MartAgent
    MartFeatures --> MartAgent
    MartQuality --> MartAgent
```

## 3. dbt Model Lineage

```mermaid
flowchart LR
    Daily["source: daily_ohlcv_base"] --> StgDaily["stg_daily_ohlcv"]
    Index["source: market_index_daily_base"] --> StgIndex["stg_market_index_daily"]
    IntraBase["source: intraday_ohlcv_15m_base"] --> StgIntra["stg_intraday_15m"]
    IntraDelta["source: intraday_ohlcv_15m_delta"] --> StgIntra
    Universe["source: universe_members"] --> StgUniverse["stg_universe_members"]
    Manifest["source: data_file_manifest"] --> StgManifest["stg_data_file_manifest"]

    StgDaily --> Returns["int_symbol_daily_returns"]
    StgDaily --> Liquidity["int_symbol_liquidity_daily"]
    StgDaily --> Breadth["int_market_breadth_daily"]
    StgIndex --> IndexReturns["int_market_index_returns"]
    StgIntra --> IntraSummary["int_intraday_daily_summary"]
    StgManifest --> Freshness["int_data_freshness_status"]

    IndexReturns --> Regime["mart_market_regime_daily"]
    Breadth --> Regime

    Returns --> SymbolFeatures["mart_symbol_daily_features"]
    Liquidity --> SymbolFeatures
    IndexReturns --> SymbolFeatures
    StgUniverse --> SymbolFeatures

    Freshness --> Quality["mart_symbol_data_quality"]
    StgUniverse --> Quality

    SymbolFeatures --> AgentContext["mart_agent_context_daily"]
    Regime --> AgentContext
    Quality --> AgentContext
```

Current dbt validation status:

| Layer | Count |
| --- | ---: |
| Models | `15` |
| Tests | `72` |
| Sources | `8` |
| Mart rows: `mart_market_regime_daily` | `2,499` |
| Mart rows: `mart_symbol_daily_features` | `69,674` |
| Mart rows: `mart_symbol_data_quality` | `30` |
| Mart rows: `mart_agent_context_daily` | `69,674` |

## 4. Live Update And EOD Merge Flow

```mermaid
sequenceDiagram
    participant Worker as live_update_worker
    participant Delta as run_intraday_delta
    participant VCI as vnstock VCI-data-source
    participant DB as warehouse/bquant.duckdb
    participant Files as Parquet files
    participant Web as NiceGUI Web App
    participant EOD as run_eod_reconcile

    Worker->>Worker: detect eligible 15m slot
    Worker->>Delta: dispatch scheduled delta job
    Delta->>DB: read checkpoint/watermark
    Delta->>VCI: fetch overlapping 15m bars
    VCI-->>Delta: standardized OHLCV rows
    Delta->>DB: upsert intraday_ohlcv_15m_delta
    Delta->>Files: materialize delta parquet per symbol
    Delta->>DB: bump dataset_refresh_state
    Web->>DB: read base + delta plot view

    EOD->>VCI: refresh current daily bars
    EOD->>DB: merge delta into intraday_ohlcv_15m_base
    EOD->>Files: regenerate base intraday files
    EOD->>DB: clear delta and bump refresh versions
    Web->>DB: reload cache on refresh_version change
```

## 5. Observability Data Flow

```mermaid
flowchart LR
    subgraph Runtime["Runtime Components"]
        WebApp["web app"]
        Pipelines["pipeline jobs"]
        Worker["live worker"]
        Alerts["alert evaluator"]
    end

    subgraph Logs["File-Based Logs"]
        JSONL["logs/observability/jsonl/*"]
        DeadLetter["logs/observability/dead_letter/*"]
    end

    subgraph ObsDB["warehouse/bquant_observability.duckdb"]
        LogEvents["obs_log_events"]
        JobRuns["obs_job_runs"]
        Requests["obs_source_requests"]
        Checkpoints["obs_live_checkpoints"]
        Heartbeats["obs_scheduler_heartbeats"]
        AlertRows["obs_alerts"]
    end

    subgraph OpsUI["Operations UI"]
        Operations["/operations"]
        AlertPage["/alerts"]
    end

    WebApp --> JSONL
    Pipelines --> JSONL
    Worker --> JSONL
    Alerts --> JSONL
    JSONL --> Ingest["ingest_observability_logs"]
    Ingest --> LogEvents
    Ingest --> JobRuns
    Ingest --> Requests
    Ingest --> Checkpoints
    Ingest --> Heartbeats
    Ingest --> AlertRows
    Ingest --> DeadLetter
    LogEvents --> Operations
    JobRuns --> Operations
    Checkpoints --> Operations
    Heartbeats --> Operations
    AlertRows --> AlertPage
```

## 6. Future News And Vector Data Extension

This part is not implemented yet. It shows where the planned news/social vector dataset should attach without disrupting the current market-data stack.

```mermaid
flowchart LR
    subgraph NewsSources["News / Social Sources"]
        News["financial news"]
        Social["forums/social posts"]
        Reports["broker reports"]
    end

    subgraph DocumentIngestion["Future Document Pipeline"]
        Crawl["crawl / ingest"]
        Clean["clean / deduplicate"]
        EntityLink["entity link<br/>VNINDEX, VN30, symbols"]
        Sentiment["sentiment / topic / event extraction"]
        Embed["embedding generation"]
    end

    subgraph DocumentStores["Future Stores"]
        RawDocs["raw_documents"]
        Chunks["document_chunks"]
        Entities["document_entities"]
        Sentiments["document_sentiment"]
        VectorStore["vector index<br/>Qdrant/LanceDB/ClickHouse"]
        SentimentMart["symbol_sentiment_daily"]
    end

    subgraph CurrentMarketStack["Current Market Stack"]
        AgentContext["analytics_marts.mart_agent_context_daily"]
        FutureAgent["Future recommendation agents"]
    end

    News --> Crawl
    Social --> Crawl
    Reports --> Crawl
    Crawl --> RawDocs --> Clean --> Chunks
    Chunks --> EntityLink --> Entities
    Chunks --> Sentiment --> Sentiments --> SentimentMart
    Chunks --> Embed --> VectorStore
    AgentContext --> FutureAgent
    SentimentMart --> FutureAgent
    VectorStore --> FutureAgent
```

Recommended implementation rule for the news/vector phase:

- keep raw document ingestion append-only
- treat embeddings as versioned derived data
- aggregate symbol/index sentiment into daily marts before joining with market features
- let future agents read `mart_agent_context_daily` plus sentiment/vector retrieval results, not raw crawler output directly
