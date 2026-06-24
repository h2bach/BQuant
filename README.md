# BQuant

BQuant is an investment research and decision-support platform for the Vietnam stock market, with `VN30` as the initial universe.

It is designed as a system that combines:

- `Data Analytics`: structured market data, technical signals, breadth, trend analysis, and data-quality awareness
- `Agentic AI`: specialized agents that analyze market context, inspect symbols, challenge assumptions, and produce explainable recommendations
- `QAOA Portfolio Optimizer`: a quantum-inspired portfolio optimization layer for allocation and portfolio recommendation under practical constraints

## What BQuant is

BQuant is not just a data pipeline, and it is not just a dashboard.

It is intended to be a multi-layer platform where:

1. `Market Data` builds a reliable local data foundation
2. `Analytics` transforms raw data into market structure, signals, and interpretable features
3. `Agentic AI` turns those signals into reasoning, narratives, and decision support
4. `Portfolio Optimization` converts opportunities into structured portfolio recommendations
5. `User Interfaces` expose charts, monitoring, exploration, and recommendation workflows

## Platform vision

At a high level, BQuant is being shaped as a research-first intelligence platform for investing:

- a data layer that continuously ingests, validates, and serves market data
- an analytics layer that measures momentum, relative strength, breadth, and technical structure
- an agent layer that can interpret market state, review symbol behavior, and synthesize recommendations
- an optimization layer that can solve constrained allocation problems using QAOA or quantum-inspired methods
- a delivery layer that presents both signals and their rationale in a way that can be reviewed and challenged

## Core pillars

### 1. Data Analytics

The analytics foundation is responsible for:

- historical and intraday OHLCV ingestion
- market data standardization
- technical indicator computation
- trend and benchmark comparison
- market breadth and signal scoring
- freshness, manifest, and observability tracking

The goal is to make market data queryable, explainable, and ready for downstream reasoning.

### 2. Agentic AI

The agentic layer is intended to go beyond static indicators.

Its role is to:

- monitor overall market structure
- inspect individual VN30 symbols
- detect anomalies, stale data, or conflicting signals
- generate interpretable investment hypotheses
- support recommendation workflows with context, not just numbers

In BQuant, agents are not meant to replace judgment. They are meant to improve it.

### 3. QAOA Portfolio Optimizer

The optimization layer is intended to take candidate signals and convert them into portfolio-level recommendations.

Its role includes:

- selecting candidate assets under constraints
- balancing return and risk objectives
- enforcing diversification and liquidity constraints
- optimizing allocation across competing opportunities
- supporting explainable portfolio construction rather than black-box picks

QAOA is part of the long-term positioning of BQuant as a platform that connects quantitative analytics with advanced optimization methods.

## What BQuant is not

BQuant is not intended to be:

- a fully automated live trading engine in the MVP stage
- a black-box stock picker
- a charting tool without a reasoning layer
- a recommendation engine without data lineage or observability

The platform is being built as a `decision-support system`, not as blind automation.

## Current starting scope

The current implementation starts from the data foundation:

- initial universe: `VN30`
- base datasets:
  - `10-year daily OHLCV`
  - `60-day intraday 15m OHLCV`
- local storage architecture:
  - `DuckDB`
  - `Parquet`
- local execution model for research, rapid iteration, and debugging
- live-update and observability capabilities to support continuous operation later

## What a recommendation should mean in BQuant

A strong recommendation in BQuant should not only answer:

- which symbol looks strong
- which signal just triggered

It should connect:

- overall market context
- symbol-specific structure
- data freshness and reliability
- portfolio-level tradeoffs
- allocation logic
- natural-language explanation from the agent layer

That is the real purpose of the platform.

## Design philosophy

BQuant is being built to be:

- `modular`: data, agents, optimization, and UI can evolve independently
- `observable`: pipeline behavior and platform health can be debugged end to end
- `research-first`: fast iteration matters, but traceability matters more
- `explainable`: every recommendation should be supported by visible reasoning
- `upgradeable`: local MVP first, more advanced live intelligence later

## In one sentence

`BQuant is an intelligent investment research platform for Vietnam equities, where Data Analytics builds the foundation, Agentic AI provides reasoning and recommendation support, and a QAOA portfolio optimizer turns signals into structured, explainable portfolio ideas.`
