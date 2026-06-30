-- BQuant DuckDB Schema
-- Database: warehouse/bquant.duckdb
-- System: BQuant - Quantitative Research Pipeline for Vietnam Stock Market

-- Parquet support is bundled with DuckDB in modern builds.
-- Keep schema bootstrap offline-safe by avoiding extension install here.

-- ============================================
-- Universe Members Table
-- ============================================
CREATE TABLE IF NOT EXISTS universe_members (
    universe_name VARCHAR NOT NULL,
    symbol VARCHAR NOT NULL,
    effective_date DATE NOT NULL,
    end_date DATE,
    source VARCHAR NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (universe_name, symbol, effective_date)
);

-- Create index for efficient queries
CREATE INDEX IF NOT EXISTS idx_universe_members_symbol ON universe_members(symbol);
CREATE INDEX IF NOT EXISTS idx_universe_members_dates ON universe_members(effective_date, end_date);

-- ============================================
-- Raw OHLCV Table
-- ============================================
CREATE TABLE IF NOT EXISTS raw_ohlcv (
    symbol VARCHAR NOT NULL,
    trading_date DATE NOT NULL,
    open DOUBLE NOT NULL,
    high DOUBLE NOT NULL,
    low DOUBLE NOT NULL,
    close DOUBLE NOT NULL,
    adjusted_close DOUBLE,
    volume DOUBLE NOT NULL,
    source VARCHAR NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Create indexes
CREATE INDEX IF NOT EXISTS idx_raw_ohlcv_symbol_date ON raw_ohlcv(symbol, trading_date);
CREATE INDEX IF NOT EXISTS idx_raw_ohlcv_date ON raw_ohlcv(trading_date);
CREATE INDEX IF NOT EXISTS idx_raw_ohlcv_source ON raw_ohlcv(source);

-- ============================================
-- Raw Hourly OHLCV Table
-- ============================================
CREATE TABLE IF NOT EXISTS raw_ohlcv_hourly (
    symbol VARCHAR NOT NULL,
    bar_time TIMESTAMP NOT NULL,
    open DOUBLE NOT NULL,
    high DOUBLE NOT NULL,
    low DOUBLE NOT NULL,
    close DOUBLE NOT NULL,
    volume DOUBLE NOT NULL,
    interval VARCHAR NOT NULL DEFAULT '1H',
    source VARCHAR NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (symbol, bar_time, source)
);

CREATE INDEX IF NOT EXISTS idx_raw_ohlcv_hourly_symbol_time ON raw_ohlcv_hourly(symbol, bar_time);
CREATE INDEX IF NOT EXISTS idx_raw_ohlcv_hourly_time ON raw_ohlcv_hourly(bar_time);
CREATE INDEX IF NOT EXISTS idx_raw_ohlcv_hourly_source ON raw_ohlcv_hourly(source);

-- ============================================
-- Clean OHLCV Daily Table
-- ============================================
CREATE TABLE IF NOT EXISTS clean_ohlcv_daily (
    symbol VARCHAR NOT NULL,
    trading_date DATE NOT NULL,
    open DOUBLE NOT NULL,
    high DOUBLE NOT NULL,
    low DOUBLE NOT NULL,
    close DOUBLE NOT NULL,
    adjusted_close DOUBLE,
    volume DOUBLE NOT NULL,
    source VARCHAR NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (symbol, trading_date)
);

-- Create indexes
CREATE INDEX IF NOT EXISTS idx_clean_ohlcv_symbol_date ON clean_ohlcv_daily(symbol, trading_date);
CREATE INDEX IF NOT EXISTS idx_clean_ohlcv_date ON clean_ohlcv_daily(trading_date);

-- ============================================
-- Clean Hourly OHLCV Table
-- ============================================
CREATE TABLE IF NOT EXISTS clean_ohlcv_hourly (
    symbol VARCHAR NOT NULL,
    bar_time TIMESTAMP NOT NULL,
    open DOUBLE NOT NULL,
    high DOUBLE NOT NULL,
    low DOUBLE NOT NULL,
    close DOUBLE NOT NULL,
    volume DOUBLE NOT NULL,
    interval VARCHAR NOT NULL DEFAULT '1H',
    source VARCHAR NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (symbol, bar_time)
);

CREATE INDEX IF NOT EXISTS idx_clean_ohlcv_hourly_symbol_time ON clean_ohlcv_hourly(symbol, bar_time);
CREATE INDEX IF NOT EXISTS idx_clean_ohlcv_hourly_time ON clean_ohlcv_hourly(bar_time);

-- ============================================
-- Base Daily OHLCV Table (10 Years)
-- ============================================
CREATE TABLE IF NOT EXISTS daily_ohlcv_base (
    symbol VARCHAR NOT NULL,
    trading_date DATE NOT NULL,
    open DOUBLE NOT NULL,
    high DOUBLE NOT NULL,
    low DOUBLE NOT NULL,
    close DOUBLE NOT NULL,
    adjusted_close DOUBLE,
    volume DOUBLE NOT NULL,
    source VARCHAR NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (symbol, trading_date)
);

CREATE INDEX IF NOT EXISTS idx_daily_ohlcv_base_symbol_date ON daily_ohlcv_base(symbol, trading_date);
CREATE INDEX IF NOT EXISTS idx_daily_ohlcv_base_date ON daily_ohlcv_base(trading_date);

-- ============================================
-- Market Index Daily OHLCV Table (10 Years)
-- ============================================
CREATE TABLE IF NOT EXISTS market_index_daily_base (
    symbol VARCHAR NOT NULL,
    trading_date DATE NOT NULL,
    open DOUBLE NOT NULL,
    high DOUBLE NOT NULL,
    low DOUBLE NOT NULL,
    close DOUBLE NOT NULL,
    adjusted_close DOUBLE,
    volume DOUBLE NOT NULL,
    source VARCHAR NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (symbol, trading_date)
);

CREATE INDEX IF NOT EXISTS idx_market_index_daily_symbol_date ON market_index_daily_base(symbol, trading_date);
CREATE INDEX IF NOT EXISTS idx_market_index_daily_date ON market_index_daily_base(trading_date);

-- ============================================
-- Base Intraday OHLCV Table (15m, 60 Days)
-- ============================================
CREATE TABLE IF NOT EXISTS intraday_ohlcv_15m_base (
    symbol VARCHAR NOT NULL,
    bar_time TIMESTAMP NOT NULL,
    session_date DATE NOT NULL,
    open DOUBLE NOT NULL,
    high DOUBLE NOT NULL,
    low DOUBLE NOT NULL,
    close DOUBLE NOT NULL,
    volume DOUBLE NOT NULL,
    interval VARCHAR NOT NULL DEFAULT '15m',
    source VARCHAR NOT NULL,
    snapshot_date DATE NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (symbol, bar_time)
);

CREATE INDEX IF NOT EXISTS idx_intraday_15m_base_symbol_time ON intraday_ohlcv_15m_base(symbol, bar_time);
CREATE INDEX IF NOT EXISTS idx_intraday_15m_base_session_date ON intraday_ohlcv_15m_base(session_date);

-- ============================================
-- Intraday Delta OHLCV Table (15m)
-- ============================================
CREATE TABLE IF NOT EXISTS intraday_ohlcv_15m_delta (
    symbol VARCHAR NOT NULL,
    bar_time TIMESTAMP NOT NULL,
    session_date DATE NOT NULL,
    open DOUBLE NOT NULL,
    high DOUBLE NOT NULL,
    low DOUBLE NOT NULL,
    close DOUBLE NOT NULL,
    volume DOUBLE NOT NULL,
    interval VARCHAR NOT NULL DEFAULT '15m',
    source VARCHAR NOT NULL,
    snapshot_date DATE NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (symbol, bar_time)
);

CREATE INDEX IF NOT EXISTS idx_intraday_15m_delta_symbol_time ON intraday_ohlcv_15m_delta(symbol, bar_time);
CREATE INDEX IF NOT EXISTS idx_intraday_15m_delta_session_date ON intraday_ohlcv_15m_delta(session_date);

-- ============================================
-- Data File Manifest Table
-- ============================================
CREATE TABLE IF NOT EXISTS data_file_manifest (
    dataset_name VARCHAR NOT NULL,
    symbol VARCHAR NOT NULL,
    file_role VARCHAR NOT NULL,
    granularity VARCHAR NOT NULL,
    interval VARCHAR,
    file_name VARCHAR NOT NULL,
    file_path VARCHAR NOT NULL,
    coverage_start TIMESTAMP,
    coverage_end TIMESTAMP,
    row_count INTEGER NOT NULL DEFAULT 0,
    file_size_bytes BIGINT NOT NULL DEFAULT 0,
    snapshot_date DATE,
    source VARCHAR NOT NULL,
    latest_expected_ts TIMESTAMP,
    update_status VARCHAR NOT NULL DEFAULT 'unknown',
    needs_merge BOOLEAN NOT NULL DEFAULT FALSE,
    notes VARCHAR,
    last_refresh_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (dataset_name, symbol, file_role)
);

CREATE INDEX IF NOT EXISTS idx_data_file_manifest_dataset ON data_file_manifest(dataset_name);
CREATE INDEX IF NOT EXISTS idx_data_file_manifest_status ON data_file_manifest(update_status);

-- ============================================
-- Technical Features Daily Table
-- ============================================
CREATE TABLE IF NOT EXISTS technical_features_daily (
    symbol VARCHAR NOT NULL,
    trading_date DATE NOT NULL,
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

-- Create indexes
CREATE INDEX IF NOT EXISTS idx_technical_features_symbol_date ON technical_features_daily(symbol, trading_date);
CREATE INDEX IF NOT EXISTS idx_technical_features_date ON technical_features_daily(trading_date);

-- ============================================
-- Combined Features Daily Table
-- ============================================
CREATE TABLE IF NOT EXISTS combined_features_daily (
    symbol VARCHAR NOT NULL,
    trading_date DATE NOT NULL,
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

-- Create indexes
CREATE INDEX IF NOT EXISTS idx_combined_features_symbol_date ON combined_features_daily(symbol, trading_date);
CREATE INDEX IF NOT EXISTS idx_combined_features_date ON combined_features_daily(trading_date);
CREATE INDEX IF NOT EXISTS idx_combined_features_technical_score ON combined_features_daily(technical_score);

-- ============================================
-- Trading Signals Table
-- ============================================
CREATE TABLE IF NOT EXISTS trading_signals (
    signal_id UUID DEFAULT uuid(),
    symbol VARCHAR NOT NULL,
    signal_date DATE NOT NULL,
    technical_score DOUBLE,
    nlp_score DOUBLE,
    market_regime_score DOUBLE,
    risk_score DOUBLE,
    final_score DOUBLE,
    signal VARCHAR NOT NULL,
    model_version VARCHAR,
    explanation VARCHAR,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (signal_id)
);

-- Create indexes
CREATE INDEX IF NOT EXISTS idx_trading_signals_symbol_date ON trading_signals(symbol, signal_date);
CREATE INDEX IF NOT EXISTS idx_trading_signals_date ON trading_signals(signal_date);
CREATE INDEX IF NOT EXISTS idx_trading_signals_signal ON trading_signals(signal);
CREATE INDEX IF NOT EXISTS idx_trading_signals_final_score ON trading_signals(final_score);

-- ============================================
-- Agent Recommendation Tables
-- ============================================
CREATE TABLE IF NOT EXISTS agent_recommendation_runs (
    run_id VARCHAR PRIMARY KEY,
    as_of_date DATE NOT NULL,
    trigger_type VARCHAR NOT NULL,
    status VARCHAR NOT NULL,
    input_rows INTEGER NOT NULL DEFAULT 0,
    output_rows INTEGER NOT NULL DEFAULT 0,
    agent_version VARCHAR NOT NULL,
    optimizer_mode VARCHAR,
    qaoa_enabled BOOLEAN NOT NULL DEFAULT FALSE,
    notes VARCHAR,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS agent_recommendations (
    run_id VARCHAR NOT NULL,
    as_of_date DATE NOT NULL,
    symbol VARCHAR NOT NULL,
    recommendation VARCHAR NOT NULL,
    confidence DOUBLE NOT NULL,
    score DOUBLE NOT NULL,
    risk_level VARCHAR NOT NULL,
    suggested_weight DOUBLE NOT NULL DEFAULT 0,
    rationale_json VARCHAR NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (run_id, symbol)
);

CREATE INDEX IF NOT EXISTS idx_agent_recommendation_runs_date ON agent_recommendation_runs(as_of_date);
CREATE INDEX IF NOT EXISTS idx_agent_recommendations_date ON agent_recommendations(as_of_date);
CREATE INDEX IF NOT EXISTS idx_agent_recommendations_symbol ON agent_recommendations(symbol);

-- ============================================
-- Agent System Analysis Tables
-- ============================================
CREATE TABLE IF NOT EXISTS agent_system_analysis_runs (
    run_id VARCHAR PRIMARY KEY,
    analysis_date DATE NOT NULL,
    trigger_type VARCHAR NOT NULL,
    status VARCHAR NOT NULL,
    report_markdown VARCHAR NOT NULL,
    sections_json VARCHAR NOT NULL,
    question VARCHAR,
    answer_markdown VARCHAR,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_agent_system_analysis_runs_date ON agent_system_analysis_runs(analysis_date);
CREATE INDEX IF NOT EXISTS idx_agent_system_analysis_runs_status ON agent_system_analysis_runs(status);

-- ============================================
-- Agent Live Chat Tables
-- ============================================
CREATE TABLE IF NOT EXISTS agent_chat_messages (
    chat_id VARCHAR PRIMARY KEY,
    event_ts TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    question VARCHAR NOT NULL,
    answer_markdown VARCHAR NOT NULL,
    answer_source VARCHAR NOT NULL,
    model VARCHAR,
    latency_seconds DOUBLE,
    sections_json VARCHAR NOT NULL,
    error_message VARCHAR
);

CREATE INDEX IF NOT EXISTS idx_agent_chat_messages_event_ts ON agent_chat_messages(event_ts);
CREATE INDEX IF NOT EXISTS idx_agent_chat_messages_source ON agent_chat_messages(answer_source);

-- ============================================
-- Agent Knowledge Base Tables
-- ============================================
CREATE TABLE IF NOT EXISTS agent_knowledge_documents (
    doc_id VARCHAR PRIMARY KEY,
    source_path VARCHAR NOT NULL,
    source_type VARCHAR NOT NULL,
    title VARCHAR NOT NULL,
    content_hash VARCHAR NOT NULL,
    char_count INTEGER NOT NULL,
    ingested_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS agent_knowledge_chunks (
    chunk_id VARCHAR PRIMARY KEY,
    doc_id VARCHAR NOT NULL,
    chunk_index INTEGER NOT NULL,
    source_path VARCHAR NOT NULL,
    source_type VARCHAR NOT NULL,
    title VARCHAR NOT NULL,
    content VARCHAR NOT NULL,
    content_hash VARCHAR NOT NULL,
    char_count INTEGER NOT NULL,
    ingested_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_agent_knowledge_chunks_doc ON agent_knowledge_chunks(doc_id);
CREATE INDEX IF NOT EXISTS idx_agent_knowledge_chunks_source ON agent_knowledge_chunks(source_type, source_path);

-- ============================================
-- Demo Trading Tables
-- ============================================
CREATE TABLE IF NOT EXISTS demo_trading_accounts (
    account_id VARCHAR PRIMARY KEY,
    account_name VARCHAR NOT NULL,
    currency VARCHAR NOT NULL DEFAULT 'VND',
    initial_cash DOUBLE NOT NULL,
    cash_balance DOUBLE NOT NULL,
    settlement_rule VARCHAR NOT NULL DEFAULT 'T+2.5',
    board_lot INTEGER NOT NULL DEFAULT 100,
    started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS demo_trading_orders (
    order_id VARCHAR PRIMARY KEY,
    account_id VARCHAR NOT NULL,
    action VARCHAR NOT NULL,
    symbol VARCHAR NOT NULL,
    trade_date DATE NOT NULL,
    trade_time TIMESTAMP NOT NULL,
    price DOUBLE NOT NULL,
    quantity BIGINT NOT NULL,
    gross_amount DOUBLE NOT NULL,
    fees DOUBLE NOT NULL DEFAULT 0,
    taxes DOUBLE NOT NULL DEFAULT 0,
    net_amount DOUBLE NOT NULL,
    realized_pnl DOUBLE NOT NULL DEFAULT 0,
    status VARCHAR NOT NULL,
    settlement_date DATE,
    settlement_ts TIMESTAMP,
    source VARCHAR NOT NULL DEFAULT 'manual',
    source_run_id VARCHAR,
    recommendation VARCHAR,
    rationale_json VARCHAR,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_demo_trading_orders_account_time ON demo_trading_orders(account_id, trade_time);
CREATE INDEX IF NOT EXISTS idx_demo_trading_orders_symbol ON demo_trading_orders(symbol);

CREATE TABLE IF NOT EXISTS demo_trading_lots (
    lot_id VARCHAR PRIMARY KEY,
    account_id VARCHAR NOT NULL,
    symbol VARCHAR NOT NULL,
    buy_order_id VARCHAR NOT NULL,
    trade_date DATE NOT NULL,
    settlement_date DATE NOT NULL,
    settlement_ts TIMESTAMP NOT NULL,
    quantity BIGINT NOT NULL,
    remaining_quantity BIGINT NOT NULL,
    avg_cost DOUBLE NOT NULL,
    status VARCHAR NOT NULL DEFAULT 'open',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_demo_trading_lots_account_symbol ON demo_trading_lots(account_id, symbol);
CREATE INDEX IF NOT EXISTS idx_demo_trading_lots_settlement ON demo_trading_lots(settlement_ts);

CREATE TABLE IF NOT EXISTS demo_trading_daily_plans (
    plan_id VARCHAR PRIMARY KEY,
    account_id VARCHAR NOT NULL,
    plan_date DATE NOT NULL,
    source_run_id VARCHAR,
    source_as_of_date DATE,
    status VARCHAR NOT NULL DEFAULT 'active',
    summary_json VARCHAR NOT NULL,
    generated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (account_id, plan_date)
);

CREATE INDEX IF NOT EXISTS idx_demo_trading_daily_plans_date ON demo_trading_daily_plans(plan_date);

CREATE TABLE IF NOT EXISTS demo_trading_plan_items (
    plan_id VARCHAR NOT NULL,
    symbol VARCHAR NOT NULL,
    proposed_action VARCHAR NOT NULL,
    recommendation VARCHAR NOT NULL,
    score DOUBLE NOT NULL DEFAULT 0,
    confidence DOUBLE NOT NULL DEFAULT 0,
    risk_level VARCHAR NOT NULL DEFAULT 'unknown',
    suggested_weight DOUBLE NOT NULL DEFAULT 0,
    current_quantity BIGINT NOT NULL DEFAULT 0,
    sellable_quantity BIGINT NOT NULL DEFAULT 0,
    latest_price DOUBLE,
    current_value DOUBLE NOT NULL DEFAULT 0,
    target_value DOUBLE NOT NULL DEFAULT 0,
    suggested_quantity BIGINT NOT NULL DEFAULT 0,
    rationale_json VARCHAR NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (plan_id, symbol)
);

CREATE INDEX IF NOT EXISTS idx_demo_trading_plan_items_action ON demo_trading_plan_items(proposed_action);

-- ============================================
-- Backtest Runs Table
-- ============================================
CREATE TABLE IF NOT EXISTS backtest_runs (
    run_id UUID DEFAULT uuid(),
    strategy_name VARCHAR NOT NULL,
    universe VARCHAR NOT NULL,
    start_date DATE NOT NULL,
    end_date DATE NOT NULL,
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

-- Create indexes
CREATE INDEX IF NOT EXISTS idx_backtest_runs_strategy ON backtest_runs(strategy_name);
CREATE INDEX IF NOT EXISTS idx_backtest_runs_dates ON backtest_runs(start_date, end_date);
CREATE INDEX IF NOT EXISTS idx_backtest_runs_created ON backtest_runs(created_at);

-- ============================================
-- Pipeline Runs Table
-- ============================================
CREATE TABLE IF NOT EXISTS pipeline_runs (
    run_id UUID DEFAULT uuid(),
    pipeline_name VARCHAR NOT NULL,
    start_time TIMESTAMP NOT NULL,
    end_time TIMESTAMP,
    status VARCHAR NOT NULL,
    input_rows INTEGER,
    output_rows INTEGER,
    error_message VARCHAR,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (run_id)
);

-- Create indexes
CREATE INDEX IF NOT EXISTS idx_pipeline_runs_name ON pipeline_runs(pipeline_name);
CREATE INDEX IF NOT EXISTS idx_pipeline_runs_status ON pipeline_runs(status);
CREATE INDEX IF NOT EXISTS idx_pipeline_runs_created ON pipeline_runs(created_at);

-- ============================================
-- Dataset Refresh State Table
-- ============================================
CREATE TABLE IF NOT EXISTS dataset_refresh_state (
    dataset_name VARCHAR PRIMARY KEY,
    refresh_version BIGINT NOT NULL DEFAULT 0,
    last_success_at TIMESTAMP,
    latest_data_ts TIMESTAMP,
    last_run_id VARCHAR,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_dataset_refresh_state_updated ON dataset_refresh_state(updated_at);

-- ============================================
-- Views for Common Queries
-- ============================================

-- View: Latest signals
CREATE OR REPLACE VIEW v_latest_signals AS
SELECT 
    symbol,
    signal_date,
    signal,
    final_score,
    technical_score,
    nlp_score,
    risk_score,
    explanation
FROM trading_signals
WHERE signal_date = (
    SELECT MAX(signal_date) FROM trading_signals
)
ORDER BY final_score DESC;

-- View: Latest agent recommendations
CREATE OR REPLACE VIEW v_latest_agent_recommendations AS
WITH latest_run AS (
    SELECT run_id
    FROM agent_recommendation_runs
    WHERE status = 'success'
    ORDER BY created_at DESC
    LIMIT 1
)
SELECT recommendations.*
FROM agent_recommendations recommendations
INNER JOIN latest_run
  ON recommendations.run_id = latest_run.run_id
ORDER BY recommendations.suggested_weight DESC, recommendations.score DESC, recommendations.symbol;

-- View: Latest agent system analysis
CREATE OR REPLACE VIEW v_latest_agent_system_analysis AS
SELECT *
FROM agent_system_analysis_runs
WHERE status = 'success'
ORDER BY created_at DESC
LIMIT 1;

-- View: Latest agent chat messages
CREATE OR REPLACE VIEW v_latest_agent_chat_messages AS
SELECT *
FROM agent_chat_messages
ORDER BY event_ts DESC
LIMIT 100;

-- View: Agent knowledge inventory
CREATE OR REPLACE VIEW v_agent_knowledge_inventory AS
SELECT
    documents.source_type,
    count(DISTINCT documents.doc_id) AS document_count,
    count(chunks.chunk_id) AS chunk_count,
    sum(documents.char_count) AS total_document_chars,
    max(documents.ingested_at) AS last_ingested_at
FROM agent_knowledge_documents documents
LEFT JOIN agent_knowledge_chunks chunks
  ON documents.doc_id = chunks.doc_id
GROUP BY documents.source_type
ORDER BY documents.source_type;

-- View: Universe members as of a date
CREATE OR REPLACE VIEW v_universe_members AS
SELECT 
    universe_name,
    symbol,
    effective_date,
    end_date,
    source
FROM universe_members
WHERE end_date IS NULL OR end_date >= CURRENT_DATE;

-- View: Clean OHLCV with latest universe
CREATE OR REPLACE VIEW v_clean_ohlcv_universe AS
SELECT 
    c.*,
    u.universe_name
FROM clean_ohlcv_daily c
INNER JOIN v_universe_members u ON c.symbol = u.symbol
WHERE u.universe_name = 'VN30';

-- View: Clean hourly OHLCV with latest universe
CREATE OR REPLACE VIEW v_clean_ohlcv_hourly_universe AS
SELECT
    c.*,
    u.universe_name
FROM clean_ohlcv_hourly c
INNER JOIN v_universe_members u ON c.symbol = u.symbol
WHERE u.universe_name = 'VN30';

-- View: Base daily OHLCV with latest universe
CREATE OR REPLACE VIEW v_daily_ohlcv_base_universe AS
SELECT
    d.*,
    u.universe_name
FROM daily_ohlcv_base d
INNER JOIN v_universe_members u ON d.symbol = u.symbol
WHERE u.universe_name = 'VN30';

-- View: Market index daily OHLCV
CREATE OR REPLACE VIEW v_market_index_daily_base AS
SELECT *
FROM market_index_daily_base
WHERE symbol IN ('VNINDEX', 'VN30');

-- View: Base intraday 15m OHLCV with latest universe
CREATE OR REPLACE VIEW v_intraday_15m_base_universe AS
SELECT
    d.*,
    u.universe_name
FROM intraday_ohlcv_15m_base d
INNER JOIN v_universe_members u ON d.symbol = u.symbol
WHERE u.universe_name = 'VN30';

-- View: Plot-ready intraday 15m OHLCV with delta rows merged on read
CREATE OR REPLACE VIEW v_intraday_15m_plot_universe AS
SELECT
    merged.*,
    u.universe_name
FROM (
    SELECT *
    FROM intraday_ohlcv_15m_base
    UNION ALL
    SELECT delta.*
    FROM intraday_ohlcv_15m_delta delta
    LEFT JOIN intraday_ohlcv_15m_base base
      ON delta.symbol = base.symbol
     AND delta.bar_time = base.bar_time
    WHERE base.symbol IS NULL
) merged
INNER JOIN v_universe_members u ON merged.symbol = u.symbol
WHERE u.universe_name = 'VN30';

-- View: Current manifest rows for plot datasets
CREATE OR REPLACE VIEW v_data_file_manifest_current AS
SELECT
    dataset_name,
    symbol,
    file_role,
    granularity,
    interval,
    file_name,
    file_path,
    coverage_start,
    coverage_end,
    row_count,
    file_size_bytes,
    snapshot_date,
    source,
    latest_expected_ts,
    update_status,
    needs_merge,
    notes,
    last_refresh_at
FROM data_file_manifest
ORDER BY dataset_name, symbol;

-- View: Technical features with latest universe
CREATE OR REPLACE VIEW v_technical_features_universe AS
SELECT 
    t.*,
    u.universe_name
FROM technical_features_daily t
INNER JOIN v_universe_members u ON t.symbol = u.symbol
WHERE u.universe_name = 'VN30';

-- View: Combined features with latest universe
CREATE OR REPLACE VIEW v_combined_features_universe AS
SELECT 
    c.*,
    u.universe_name
FROM combined_features_daily c
INNER JOIN v_universe_members u ON c.symbol = u.symbol
WHERE u.universe_name = 'VN30';

-- View: Backtest summary
CREATE OR REPLACE VIEW v_backtest_summary AS
SELECT 
    strategy_name,
    universe,
    start_date,
    end_date,
    total_return,
    annualized_return,
    sharpe,
    max_drawdown,
    win_rate,
    created_at
FROM backtest_runs
ORDER BY created_at DESC;

-- ============================================
-- External Tables for Parquet Files
-- ============================================

-- These will be created dynamically based on configs/dataset_registry.yaml
-- Example:
-- CREATE OR REPLACE VIEW ext_raw_ohlcv AS 
-- SELECT * FROM read_parquet('data/raw/ohlcv/source=vnquant/*.parquet');

-- ============================================
-- Metadata Table
-- ============================================
CREATE TABLE IF NOT EXISTS metadata (
    key VARCHAR PRIMARY KEY,
    value VARCHAR,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Insert system metadata
INSERT OR REPLACE INTO metadata (key, value) VALUES 
    ('system_name', 'BQuant'),
    ('system_version', '0.1.0'),
    ('schema_version', '2.0'),
    ('created_date', CURRENT_DATE);

-- ============================================
-- Grant statements (if needed for multi-user)
-- ============================================
-- Not applicable for single-user embedded DuckDB
