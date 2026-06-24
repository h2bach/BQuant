-- BQuant Observability Schema

CREATE TABLE IF NOT EXISTS obs_log_events (
    event_id VARCHAR PRIMARY KEY,
    event_ts TIMESTAMP NOT NULL,
    level VARCHAR NOT NULL,
    component VARCHAR NOT NULL,
    subcomponent VARCHAR,
    channel VARCHAR NOT NULL,
    event_type VARCHAR NOT NULL,
    run_id VARCHAR,
    trigger_type VARCHAR,
    dataset_name VARCHAR,
    symbol VARCHAR,
    status VARCHAR,
    message VARCHAR NOT NULL,
    payload_json VARCHAR,
    source_file VARCHAR,
    source_line_number BIGINT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_obs_log_events_ts ON obs_log_events(event_ts);
CREATE INDEX IF NOT EXISTS idx_obs_log_events_component ON obs_log_events(component, subcomponent);
CREATE INDEX IF NOT EXISTS idx_obs_log_events_run_id ON obs_log_events(run_id);
CREATE INDEX IF NOT EXISTS idx_obs_log_events_level ON obs_log_events(level);

CREATE TABLE IF NOT EXISTS obs_job_runs (
    run_id VARCHAR PRIMARY KEY,
    pipeline_name VARCHAR NOT NULL,
    trigger_type VARCHAR,
    status VARCHAR NOT NULL,
    start_time TIMESTAMP,
    end_time TIMESTAMP,
    duration_seconds DOUBLE,
    dataset_name VARCHAR,
    input_rows BIGINT,
    output_rows BIGINT,
    error_message VARCHAR,
    source_event_id VARCHAR,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_obs_job_runs_pipeline ON obs_job_runs(pipeline_name, end_time);
CREATE INDEX IF NOT EXISTS idx_obs_job_runs_status ON obs_job_runs(status);

CREATE TABLE IF NOT EXISTS obs_source_requests (
    request_id VARCHAR PRIMARY KEY,
    run_id VARCHAR,
    dataset_name VARCHAR,
    symbol VARCHAR,
    provider VARCHAR,
    request_start TIMESTAMP,
    request_end TIMESTAMP,
    duration_seconds DOUBLE,
    status VARCHAR,
    rows_fetched BIGINT,
    wait_seconds DOUBLE,
    empty_payload BOOLEAN DEFAULT FALSE,
    error_type VARCHAR,
    error_message VARCHAR,
    source_event_id VARCHAR,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_obs_source_requests_run_id ON obs_source_requests(run_id);
CREATE INDEX IF NOT EXISTS idx_obs_source_requests_symbol ON obs_source_requests(symbol, request_end);

CREATE TABLE IF NOT EXISTS obs_live_checkpoints (
    dataset_name VARCHAR NOT NULL,
    symbol VARCHAR NOT NULL,
    watermark_ts TIMESTAMP,
    last_run_id VARCHAR,
    trigger_type VARCHAR,
    status VARCHAR,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (dataset_name, symbol)
);

CREATE INDEX IF NOT EXISTS idx_obs_live_checkpoints_updated ON obs_live_checkpoints(updated_at);

CREATE TABLE IF NOT EXISTS obs_scheduler_heartbeats (
    heartbeat_id VARCHAR PRIMARY KEY,
    worker_name VARCHAR NOT NULL,
    heartbeat_ts TIMESTAMP NOT NULL,
    session_state VARCHAR,
    trigger_type VARCHAR,
    status VARCHAR,
    payload_json VARCHAR,
    source_event_id VARCHAR,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_obs_scheduler_heartbeats_worker ON obs_scheduler_heartbeats(worker_name, heartbeat_ts);

CREATE TABLE IF NOT EXISTS obs_alerts (
    alert_key VARCHAR PRIMARY KEY,
    alert_type VARCHAR NOT NULL,
    severity VARCHAR NOT NULL,
    dataset_name VARCHAR,
    symbol VARCHAR,
    status VARCHAR NOT NULL,
    message VARCHAR NOT NULL,
    first_event_ts TIMESTAMP,
    last_event_ts TIMESTAMP,
    acknowledged_at TIMESTAMP,
    resolved_at TIMESTAMP,
    last_run_id VARCHAR,
    last_event_id VARCHAR,
    payload_json VARCHAR,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_obs_alerts_status ON obs_alerts(status, severity);

CREATE TABLE IF NOT EXISTS obs_ingestion_offsets (
    file_path VARCHAR PRIMARY KEY,
    last_line_number BIGINT NOT NULL DEFAULT 0,
    last_ingested_at TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS obs_dead_letter_events (
    dead_letter_id VARCHAR PRIMARY KEY,
    file_path VARCHAR,
    line_number BIGINT,
    raw_line VARCHAR,
    error_message VARCHAR,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_obs_dead_letter_file ON obs_dead_letter_events(file_path, line_number);

CREATE OR REPLACE VIEW v_obs_active_alerts AS
SELECT
    alert_key,
    alert_type,
    severity,
    dataset_name,
    symbol,
    status,
    message,
    first_event_ts,
    last_event_ts,
    acknowledged_at,
    resolved_at,
    last_run_id,
    updated_at
FROM obs_alerts
WHERE status IN ('open', 'acknowledged')
ORDER BY
    CASE severity
        WHEN 'CRITICAL' THEN 1
        WHEN 'ERROR' THEN 2
        WHEN 'WARNING' THEN 3
        ELSE 4
    END,
    updated_at DESC;

CREATE OR REPLACE VIEW v_obs_recent_failures AS
SELECT
    event_ts,
    component,
    subcomponent,
    event_type,
    run_id,
    dataset_name,
    symbol,
    status,
    message
FROM obs_log_events
WHERE level IN ('ERROR', 'CRITICAL')
ORDER BY event_ts DESC;

CREATE OR REPLACE VIEW v_obs_latest_job_status AS
SELECT *
FROM (
    SELECT
        pipeline_name,
        run_id,
        trigger_type,
        status,
        start_time,
        end_time,
        duration_seconds,
        dataset_name,
        input_rows,
        output_rows,
        error_message,
        updated_at,
        row_number() OVER (PARTITION BY pipeline_name ORDER BY coalesce(end_time, updated_at) DESC) AS rn
    FROM obs_job_runs
) ranked
WHERE rn = 1;

CREATE OR REPLACE VIEW v_obs_dataset_freshness AS
SELECT
    dataset_name,
    count(*) AS symbol_count,
    min(watermark_ts) AS oldest_watermark_ts,
    max(watermark_ts) AS newest_watermark_ts,
    max(updated_at) AS last_checkpoint_update
FROM obs_live_checkpoints
GROUP BY dataset_name
ORDER BY dataset_name;

CREATE OR REPLACE VIEW v_obs_symbol_lag AS
SELECT
    dataset_name,
    symbol,
    watermark_ts,
    updated_at
FROM obs_live_checkpoints
ORDER BY dataset_name, symbol;

CREATE OR REPLACE VIEW v_obs_worker_health AS
SELECT
    worker_name,
    max(heartbeat_ts) AS last_heartbeat_ts,
    arg_max(session_state, heartbeat_ts) AS last_session_state,
    arg_max(status, heartbeat_ts) AS last_status
FROM obs_scheduler_heartbeats
GROUP BY worker_name
ORDER BY worker_name;
