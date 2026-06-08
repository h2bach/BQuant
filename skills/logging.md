# Skill: Logging

## Purpose

Use this skill whenever implementing or reviewing logging for BQuant pipelines, data ingestion, feature engineering, backtesting, or any automated processes.

## Logging Philosophy

All data operations in BQuant must be logged with sufficient detail to:
- Debug data quality issues
- Track pipeline execution history
- Monitor data freshness and completeness
- Audit data lineage and transformations
- Identify performance bottlenecks

## Log Directory Structure

```text
logs/
├── data/
│   ├── ingestion/
│   │   ├── ohlcv/
│   │   │   ├── vnquant_YYYYMMDD.log
│   │   │   └── vnquant_YYYYMMDD.jsonl
│   │   └── universe/
│   │       └── vn30_YYYYMMDD.log
│   ├── features/
│   │   ├── technical_YYYYMMDD.log
│   │   └── combined_YYYYMMDD.log
│   ├── backtesting/
│   │   └── backtest_YYYYMMDD.jsonl
│   └── signals/
│       └── signal_generation_YYYYMMDD.log
├── pipeline/
│   ├── daily_update_ohlcv_YYYYMMDD.log
│   ├── daily_feature_pipeline_YYYYMMDD.log
│   └── daily_signal_pipeline_YYYYMMDD.log
└── errors/
    └── error_YYYYMMDD.log
```

## Required Logging Modules

Create `utils/logger.py` with:

```python
import logging
import json
from pathlib import Path
from datetime import datetime
from typing import Dict, Any, Optional
import sys

class BQuantLogger:
    """Centralized logger for BQuant pipelines."""
    
    def __init__(self, name: str, log_dir: str = "logs"):
        self.name = name
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        
        # Create subdirectories
        (self.log_dir / "data").mkdir(exist_ok=True)
        (self.log_dir / "pipeline").mkdir(exist_ok=True)
        (self.log_dir / "errors").mkdir(exist_ok=True)
        
        self.logger = self._setup_logger()
        
    def _setup_logger(self) -> logging.Logger:
        logger = logging.getLogger(self.name)
        logger.setLevel(logging.DEBUG)
        
        # Clear existing handlers
        logger.handlers.clear()
        
        # Console handler
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setLevel(logging.INFO)
        console_format = logging.Formatter(
            '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
        )
        console_handler.setFormatter(console_format)
        logger.addHandler(console_handler)
        
        return logger
    
    def get_log_file_path(self, category: str, extension: str = "log") -> Path:
        """Get log file path with date."""
        date_str = datetime.now().strftime("%Y%m%d")
        filename = f"{self.name}_{date_str}.{extension}"
        
        if category == "data":
            return self.log_dir / "data" / filename
        elif category == "pipeline":
            return self.log_dir / "pipeline" / filename
        elif category == "error":
            return self.log_dir / "errors" / filename
        else:
            return self.log_dir / filename
    
    def log_data_ingestion(
        self,
        source: str,
        symbols: list[str],
        start_date: str,
        end_date: str,
        rows_fetched: int,
        rows_saved: int,
        duration_seconds: float,
        status: str = "success",
        error_message: Optional[str] = None
    ) -> None:
        """Log data ingestion operation."""
        log_entry = {
            "timestamp": datetime.now().isoformat(),
            "operation": "data_ingestion",
            "source": source,
            "symbols": symbols,
            "start_date": start_date,
            "end_date": end_date,
            "rows_fetched": rows_fetched,
            "rows_saved": rows_saved,
            "duration_seconds": duration_seconds,
            "status": status,
            "error_message": error_message
        }
        
        # Log to file (JSONL format)
        log_file = self.get_log_file_path("data", "jsonl")
        with open(log_file, "a") as f:
            f.write(json.dumps(log_entry) + "\n")
        
        # Log to console
        if status == "success":
            self.logger.info(
                f"Ingestion from {source}: {rows_saved} rows saved for {len(symbols)} symbols "
                f"({start_date} to {end_date}) in {duration_seconds:.2f}s"
            )
        else:
            self.logger.error(
                f"Ingestion from {source} failed: {error_message}"
            )
    
    def log_feature_computation(
        self,
        feature_type: str,
        symbols: list[str],
        input_rows: int,
        output_rows: int,
        features_computed: list[str],
        duration_seconds: float,
        status: str = "success",
        error_message: Optional[str] = None
    ) -> None:
        """Log feature computation operation."""
        log_entry = {
            "timestamp": datetime.now().isoformat(),
            "operation": "feature_computation",
            "feature_type": feature_type,
            "symbols": symbols,
            "input_rows": input_rows,
            "output_rows": output_rows,
            "features_computed": features_computed,
            "duration_seconds": duration_seconds,
            "status": status,
            "error_message": error_message
        }
        
        log_file = self.get_log_file_path("data", "jsonl")
        with open(log_file, "a") as f:
            f.write(json.dumps(log_entry) + "\n")
        
        if status == "success":
            self.logger.info(
                f"Feature computation ({feature_type}): {output_rows} rows, "
                f"{len(features_computed)} features in {duration_seconds:.2f}s"
            )
        else:
            self.logger.error(
                f"Feature computation ({feature_type}) failed: {error_message}"
            )
    
    def log_backtest(
        self,
        strategy_name: str,
        universe: str,
        start_date: str,
        end_date: str,
        total_return: float,
        sharpe: float,
        max_drawdown: float,
        duration_seconds: float,
        status: str = "success",
        error_message: Optional[str] = None
    ) -> None:
        """Log backtest operation."""
        log_entry = {
            "timestamp": datetime.now().isoformat(),
            "operation": "backtest",
            "strategy_name": strategy_name,
            "universe": universe,
            "start_date": start_date,
            "end_date": end_date,
            "total_return": total_return,
            "sharpe": sharpe,
            "max_drawdown": max_drawdown,
            "duration_seconds": duration_seconds,
            "status": status,
            "error_message": error_message
        }
        
        log_file = self.get_log_file_path("data", "jsonl")
        with open(log_file, "a") as f:
            f.write(json.dumps(log_entry) + "\n")
        
        if status == "success":
            self.logger.info(
                f"Backtest ({strategy_name}): Return={total_return:.2%}, "
                f"Sharpe={sharpe:.2f}, MaxDD={max_drawdown:.2%} in {duration_seconds:.2f}s"
            )
        else:
            self.logger.error(
                f"Backtest ({strategy_name}) failed: {error_message}"
            )
    
    def log_pipeline_run(
        self,
        pipeline_name: str,
        start_time: str,
        end_time: str,
        status: str,
        steps_completed: list[str],
        steps_failed: list[str],
        error_message: Optional[str] = None
    ) -> None:
        """Log pipeline execution."""
        log_entry = {
            "timestamp": datetime.now().isoformat(),
            "operation": "pipeline_run",
            "pipeline_name": pipeline_name,
            "start_time": start_time,
            "end_time": end_time,
            "status": status,
            "steps_completed": steps_completed,
            "steps_failed": steps_failed,
            "error_message": error_message
        }
        
        log_file = self.get_log_file_path("pipeline", "jsonl")
        with open(log_file, "a") as f:
            f.write(json.dumps(log_entry) + "\n")
        
        if status == "success":
            self.logger.info(
                f"Pipeline ({pipeline_name}) completed: {len(steps_completed)} steps"
            )
        else:
            self.logger.error(
                f"Pipeline ({pipeline_name}) failed: {error_message}"
            )
    
    def log_error(
        self,
        operation: str,
        error_type: str,
        error_message: str,
        context: Optional[Dict[str, Any]] = None
    ) -> None:
        """Log error with context."""
        log_entry = {
            "timestamp": datetime.now().isoformat(),
            "operation": operation,
            "error_type": error_type,
            "error_message": error_message,
            "context": context or {}
        }
        
        log_file = self.get_log_file_path("error", "jsonl")
        with open(log_file, "a") as f:
            f.write(json.dumps(log_entry) + "\n")
        
        self.logger.error(
            f"Error in {operation} ({error_type}): {error_message}"
        )
```

## Required Logging for Data Ingestion

When crawling or ingesting data, log:

```python
logger = BQuantLogger("vnquant_ohlcv")

# Before ingestion
logger.logger.info(f"Starting ingestion for {len(symbols)} symbols from {start_date} to {end_date}")

# Per-symbol progress
for symbol in symbols:
    logger.logger.debug(f"Fetching {symbol}...")
    # ... fetch data ...
    logger.logger.debug(f"Fetched {symbol}: {len(df)} rows")

# After ingestion
logger.log_data_ingestion(
    source="vnquant",
    symbols=symbols,
    start_date=start_date,
    end_date=end_date,
    rows_fetched=total_rows,
    rows_saved=saved_rows,
    duration_seconds=duration,
    status="success"
)
```

## Required Logging for Feature Engineering

When computing features, log:

```python
logger = BQuantLogger("technical_features")

logger.log_feature_computation(
    feature_type="technical",
    symbols=symbols,
    input_rows=len(clean_ohlcv),
    output_rows=len(features_df),
    features_computed=feature_list,
    duration_seconds=duration,
    status="success"
)
```

## Required Logging for Backtesting

When running backtests, log:

```python
logger = BQuantLogger("backtest")

logger.log_backtest(
    strategy_name="top_k_technical",
    universe="VN30",
    start_date=start_date,
    end_date=end_date,
    total_return=metrics["total_return"],
    sharpe=metrics["sharpe"],
    max_drawdown=metrics["max_drawdown"],
    duration_seconds=duration,
    status="success"
)
```

## Required Logging for Pipeline Orchestration

When running pipelines, log:

```python
logger = BQuantLogger("daily_update_pipeline")

start_time = datetime.now().isoformat()
steps_completed = []
steps_failed = []

try:
    # Step 1: Ingest OHLCV
    # ...
    steps_completed.append("ingest_ohlcv")
    
    # Step 2: Validate OHLCV
    # ...
    steps_completed.append("validate_ohlcv")
    
    # Step 3: Compute features
    # ...
    steps_completed.append("compute_features")
    
except Exception as e:
    steps_failed.append(last_step)
    logger.log_error("daily_update_pipeline", type(e).__name__, str(e))
    
finally:
    end_time = datetime.now().isoformat()
    logger.log_pipeline_run(
        pipeline_name="daily_update",
        start_time=start_time,
        end_time=end_time,
        status="success" if not steps_failed else "failed",
        steps_completed=steps_completed,
        steps_failed=steps_failed
    )
```

## Log Format Standards

### JSONL Format for Structured Logs

Use JSONL (one JSON object per line) for structured logs:
- Easy to parse and query
- Supports complex nested data
- Can be loaded into DuckDB for analysis

### Text Format for Human-Readable Logs

Use text format for:
- Console output during development
- Quick debugging
- Human review

### Log Levels

- **DEBUG**: Detailed diagnostic information (per-symbol progress, intermediate steps)
- **INFO**: General operational information (pipeline start/complete, summary statistics)
- **WARNING**: Unexpected but recoverable issues (missing data for some symbols, API rate limits)
- **ERROR**: Failure that prevents operation completion (API failures, data validation errors)
- **CRITICAL**: System-level failures (database corruption, configuration errors)

## Log Retention

Recommended retention policy:
- Keep detailed logs (JSONL) for 90 days
- Keep error logs for 180 days
- Archive older logs to compressed storage if needed

## Log Analysis

Logs can be analyzed using DuckDB:

```sql
-- Load JSONL logs into DuckDB
CREATE TABLE ingestion_logs AS 
SELECT * FROM read_json('logs/data/*.jsonl', auto_detect=true);

-- Analyze ingestion success rate
SELECT 
    source,
    status,
    COUNT(*) as count,
    AVG(rows_saved) as avg_rows_saved,
    AVG(duration_seconds) as avg_duration
FROM ingestion_logs
WHERE operation = 'data_ingestion'
GROUP BY source, status;
```

## Rules

1. **All data operations must be logged**: ingestion, validation, feature computation, backtesting
2. **Use structured JSONL for machine-readable logs**: enables analysis and debugging
3. **Include timing information**: duration_seconds for all operations
4. **Include row counts**: input_rows, output_rows for data transformations
5. **Log errors with context**: operation, error_type, error_message, and relevant context
6. **Separate log categories**: data/, pipeline/, errors/ for organized storage
7. **Date-based log files**: YYYYMMDD suffix for easy rotation and querying
8. **Log to both file and console**: file for persistence, console for real-time monitoring
9. **Do not log sensitive data**: API keys, credentials, personal information
10. **Log before and after critical operations**: enable tracking of pipeline state
