"""Centralized structured logging utilities for BQuant."""

from __future__ import annotations

import json
import logging
import sys
import uuid
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

import yaml


REPO_ROOT = Path(__file__).resolve().parent.parent
BQUANT_CONFIG_PATH = REPO_ROOT / "configs" / "bquant.yaml"
OBSERVABILITY_CONFIG_PATH = REPO_ROOT / "configs" / "observability.yaml"


def _load_yaml(path: Path) -> dict[str, Any]:
    """Load a YAML file into a dictionary, returning an empty mapping if absent."""
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def _load_logging_config() -> dict[str, Any]:
    """Load base logging paths and levels from the main BQuant config."""
    config = _load_yaml(BQUANT_CONFIG_PATH)
    return config.get(
        "logging",
        {
            "base_dir": "logs",
            "data_dir": "logs/data",
            "pipeline_dir": "logs/pipeline",
            "error_dir": "logs/errors",
            "level": "INFO",
        },
    )


def _load_observability_config() -> dict[str, Any]:
    """Load observability-specific channel paths and retention locations."""
    config = _load_yaml(OBSERVABILITY_CONFIG_PATH)
    return config or {
        "logs": {
            "base_dir": "logs/observability",
            "jsonl_dir": "logs/observability/jsonl",
            "dead_letter_dir": "logs/observability/dead_letter",
            "parquet_dir": "logs/observability/parquet",
            "channels": {
                "scheduler": "logs/observability/jsonl/scheduler",
                "pipeline": "logs/observability/jsonl/pipeline",
                "ingestion": "logs/observability/jsonl/ingestion",
                "web": "logs/observability/jsonl/web",
                "alerts": "logs/observability/jsonl/alerts",
            },
        }
    }


@dataclass(frozen=True)
class LogPaths:
    """Resolved filesystem locations used by both legacy logs and observability logs."""
    base_dir: Path
    data_dir: Path
    pipeline_dir: Path
    error_dir: Path
    observability_base_dir: Path
    observability_jsonl_dir: Path
    observability_parquet_dir: Path
    observability_dead_letter_dir: Path
    observability_channels: dict[str, Path]


class BQuantLogger:
    """Structured logger with backward-compatible helpers for BQuant."""

    def __init__(
        self,
        name: str,
        *,
        component: str | None = None,
        subcomponent: str | None = None,
        default_channel: str | None = None,
        default_context: dict[str, Any] | None = None,
        _shared_logger: logging.Logger | None = None,
        _shared_paths: LogPaths | None = None,
        _shared_config: dict[str, Any] | None = None,
        _shared_obs_config: dict[str, Any] | None = None,
    ) -> None:
        """Build a structured logger with optional shared context and shared handlers."""
        self.name = name
        self.config = _shared_config or _load_logging_config()
        self.obs_config = _shared_obs_config or _load_observability_config()
        self.paths = _shared_paths or self._build_paths()
        self.component = component or self._infer_component(name)
        self.subcomponent = subcomponent or name
        self.default_channel = default_channel or self._default_channel_for_component(self.component)
        self.default_context = default_context or {}
        self._ensure_directories()
        self.logger = _shared_logger or self._setup_logger()

    def _build_paths(self) -> LogPaths:
        """Resolve every configured logging path once per logger family."""
        obs_logs = self.obs_config.get("logs", {})
        obs_channels_cfg = obs_logs.get("channels", {})
        observability_channels = {
            channel: (REPO_ROOT / path).resolve()
            for channel, path in obs_channels_cfg.items()
        }
        return LogPaths(
            base_dir=(REPO_ROOT / self.config.get("base_dir", "logs")).resolve(),
            data_dir=(REPO_ROOT / self.config.get("data_dir", "logs/data")).resolve(),
            pipeline_dir=(REPO_ROOT / self.config.get("pipeline_dir", "logs/pipeline")).resolve(),
            error_dir=(REPO_ROOT / self.config.get("error_dir", "logs/errors")).resolve(),
            observability_base_dir=(REPO_ROOT / obs_logs.get("base_dir", "logs/observability")).resolve(),
            observability_jsonl_dir=(REPO_ROOT / obs_logs.get("jsonl_dir", "logs/observability/jsonl")).resolve(),
            observability_parquet_dir=(REPO_ROOT / obs_logs.get("parquet_dir", "logs/observability/parquet")).resolve(),
            observability_dead_letter_dir=(REPO_ROOT / obs_logs.get("dead_letter_dir", "logs/observability/dead_letter")).resolve(),
            observability_channels=observability_channels,
        )

    def _ensure_directories(self) -> None:
        """Create every directory the logger may need before the first event is written."""
        directories = [
            self.paths.base_dir,
            self.paths.data_dir,
            self.paths.pipeline_dir,
            self.paths.error_dir,
            self.paths.data_dir / "ingestion",
            self.paths.data_dir / "validation",
            self.paths.data_dir / "features",
            self.paths.data_dir / "backtesting",
            self.paths.data_dir / "signals",
            self.paths.observability_base_dir,
            self.paths.observability_jsonl_dir,
            self.paths.observability_parquet_dir,
            self.paths.observability_dead_letter_dir,
        ]
        directories.extend(self.paths.observability_channels.values())
        for directory in directories:
            directory.mkdir(parents=True, exist_ok=True)

    def _setup_logger(self) -> logging.Logger:
        """Create the console logger used alongside JSONL/text file outputs."""
        logger = logging.getLogger(f"bquant.{self.component}.{self.subcomponent}")
        logger.setLevel(getattr(logging, self.config.get("level", "INFO").upper(), logging.INFO))
        logger.propagate = False
        if logger.handlers:
            logger.handlers.clear()

        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setLevel(logger.level)
        console_handler.setFormatter(
            logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s")
        )
        logger.addHandler(console_handler)
        return logger

    @staticmethod
    def _infer_component(name: str) -> str:
        lowered = name.lower()
        if lowered.startswith("web"):
            return "web"
        if "alert" in lowered:
            return "alerts"
        if "scheduler" in lowered or "worker" in lowered:
            return "scheduler"
        if lowered.startswith("fetch_") or "ingest" in lowered or "manifest" in lowered:
            return "pipeline"
        return "pipeline"

    @staticmethod
    def _default_channel_for_component(component: str) -> str:
        mapping = {
            "web": "web",
            "alerts": "alerts",
            "scheduler": "scheduler",
            "pipeline": "pipeline",
        }
        return mapping.get(component, "pipeline")

    def with_run_context(self, **context: Any) -> BQuantLogger:
        """Return a lightweight child logger that carries merged default context."""
        merged_context = {**self.default_context, **context}
        return BQuantLogger(
            self.name,
            component=self.component,
            subcomponent=self.subcomponent,
            default_channel=self.default_channel,
            default_context=merged_context,
            _shared_logger=self.logger,
            _shared_paths=self.paths,
            _shared_config=self.config,
            _shared_obs_config=self.obs_config,
        )

    def _dated_path(self, directory: Path, extension: str) -> Path:
        date_str = datetime.now().strftime("%Y%m%d")
        return directory / f"{self.name}_{date_str}.{extension}"

    def _json_default(self, value: Any) -> str:
        if isinstance(value, (datetime, date)):
            return value.isoformat()
        if isinstance(value, Path):
            return str(value)
        return str(value)

    def _serialize_payload(self, payload: dict[str, Any]) -> str:
        return json.dumps(payload, ensure_ascii=True, default=self._json_default, sort_keys=True)

    def _channel_dir(self, channel: str) -> Path:
        if channel not in self.paths.observability_channels:
            raise ValueError(f"Unsupported observability channel: {channel}")
        return self.paths.observability_channels[channel]

    def _write_jsonl(self, directory: Path, payload: dict[str, Any]) -> Path:
        log_path = self._dated_path(directory, "jsonl")
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=True, default=self._json_default) + "\n")
        return log_path

    def _write_text(self, directory: Path, message: str) -> Path:
        log_path = self._dated_path(directory, "log")
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(f"{datetime.now().isoformat()} {message}\n")
        return log_path

    def _write_legacy_logs(
        self,
        *,
        legacy_category: str,
        message: str,
        payload: dict[str, Any],
        data_subcategory: str | None = None,
    ) -> None:
        if legacy_category == "pipeline":
            directory = self.paths.pipeline_dir
        elif legacy_category == "error":
            directory = self.paths.error_dir
        elif legacy_category == "data":
            directory = self.paths.data_dir / (data_subcategory or "ingestion")
            directory.mkdir(parents=True, exist_ok=True)
        else:
            directory = self.paths.pipeline_dir
        self._write_jsonl(directory, payload)
        self._write_text(directory, message)

    def emit_event(
        self,
        message: str,
        *,
        level: int = logging.INFO,
        channel: str | None = None,
        event_type: str | None = None,
        status: str | None = None,
        legacy_category: str = "pipeline",
        data_subcategory: str | None = None,
        **payload: Any,
    ) -> dict[str, Any]:
        """Write one structured event to observability channels and legacy text/JSON logs."""
        merged_payload = {**self.default_context, **payload}
        now = datetime.now()

        run_id = merged_payload.get("run_id")
        trigger_type = merged_payload.get("trigger_type")
        dataset_name = merged_payload.get("dataset_name") or merged_payload.get("dataset")
        symbol = merged_payload.get("symbol")
        subcomponent = str(merged_payload.get("subcomponent", self.subcomponent))
        resolved_event_type = str(
            event_type or merged_payload.get("event_type") or merged_payload.get("operation") or "log"
        )
        resolved_channel = channel or str(merged_payload.get("channel") or self.default_channel)
        resolved_status = (
            status
            or merged_payload.get("status")
            or ("failed" if level >= logging.ERROR else "warning" if level >= logging.WARNING else "success")
        )
        level_name = logging.getLevelName(level)
        event_id = str(merged_payload.get("event_id") or uuid.uuid4())

        payload_json = self._serialize_payload(merged_payload)
        event = {
            "event_id": event_id,
            "event_ts": now.isoformat(),
            "timestamp": now.isoformat(),
            "level": level_name,
            "component": self.component,
            "subcomponent": subcomponent,
            "channel": resolved_channel,
            "event_type": resolved_event_type,
            "run_id": run_id,
            "trigger_type": trigger_type,
            "dataset_name": dataset_name,
            "symbol": symbol,
            "status": resolved_status,
            "message": message,
            "payload_json": payload_json,
            "payload": merged_payload,
            "logger_name": self.name,
        }

        channel_dir = self._channel_dir(resolved_channel)
        self._write_jsonl(channel_dir, event)
        self._write_text(channel_dir, message)
        self._write_legacy_logs(
            legacy_category=legacy_category,
            message=message,
            payload={**event, **merged_payload},
            data_subcategory=data_subcategory,
        )
        self.logger.log(level, message)
        return event

    def debug(self, message: str, **payload: Any) -> dict[str, Any]:
        return self.emit_event(message, level=logging.DEBUG, **payload)

    def info(self, message: str, **payload: Any) -> dict[str, Any]:
        return self.emit_event(message, level=logging.INFO, **payload)

    def warning(self, message: str, **payload: Any) -> dict[str, Any]:
        legacy_category = "error" if self.component == "web" else "pipeline"
        return self.emit_event(message, level=logging.WARNING, legacy_category=legacy_category, **payload)

    def error(self, message: str, **payload: Any) -> dict[str, Any]:
        return self.emit_event(message, level=logging.ERROR, legacy_category="error", **payload)

    def critical(self, message: str, **payload: Any) -> dict[str, Any]:
        return self.emit_event(message, level=logging.CRITICAL, legacy_category="error", **payload)

    def log_data_event(self, subcategory: str, message: str, **payload: Any) -> dict[str, Any]:
        """Emit a data-ingestion or validation event with the correct channel routing."""
        channel = "ingestion" if subcategory in {"ingestion", "validation"} else "pipeline"
        return self.emit_event(
            message,
            level=logging.INFO,
            channel=channel,
            legacy_category="data",
            data_subcategory=subcategory,
            **payload,
        )

    def log_web_event(self, message: str, **payload: Any) -> dict[str, Any]:
        """Emit an informational web event into the web observability channel."""
        return self.emit_event(
            message,
            level=logging.INFO,
            channel="web",
            legacy_category="pipeline",
            **payload,
        )

    def log_scheduler_event(self, message: str, **payload: Any) -> dict[str, Any]:
        """Emit a scheduler/worker event into the scheduler observability channel."""
        return self.emit_event(
            message,
            level=logging.INFO,
            channel="scheduler",
            legacy_category="pipeline",
            **payload,
        )

    def log_alert_event(self, message: str, **payload: Any) -> dict[str, Any]:
        """Emit an alert lifecycle event with severity derived from alert status."""
        return self.emit_event(
            message,
            level=logging.WARNING if payload.get("status") == "open" else logging.INFO,
            channel="alerts",
            legacy_category="pipeline",
            **payload,
        )

    def log_pipeline_run(
        self,
        pipeline_name: str,
        start_time: str,
        end_time: str,
        status: str,
        steps_completed: list[str],
        steps_failed: list[str],
        error_message: str | None = None,
        **extra: Any,
    ) -> dict[str, Any]:
        """Emit a normalized pipeline-run summary event with status-aware severity."""
        message = (
            f"Pipeline {pipeline_name} finished with status={status}; "
            f"completed={len(steps_completed)} failed={len(steps_failed)}"
        )
        status_level_map = {
            "success": logging.INFO,
            "running": logging.INFO,
            "skipped": logging.INFO,
            "no_data": logging.WARNING,
            "warning": logging.WARNING,
            "failed": logging.ERROR,
            "error": logging.ERROR,
            "critical": logging.CRITICAL,
        }
        resolved_level = status_level_map.get(status, logging.INFO)
        return self.emit_event(
            message,
            level=resolved_level,
            channel="pipeline",
            event_type="pipeline_run",
            status=status,
            legacy_category="error" if resolved_level >= logging.ERROR else "pipeline",
            pipeline_name=pipeline_name,
            start_time=start_time,
            end_time=end_time,
            steps_completed=steps_completed,
            steps_failed=steps_failed,
            error_message=error_message,
            **extra,
        )

    def log_error(
        self,
        operation: str,
        error_type: str,
        error_message: str,
        *,
        context: dict[str, Any] | None = None,
        channel: str | None = None,
    ) -> dict[str, Any]:
        """Emit a structured error event with operation, type, and context payload."""
        payload = context.copy() if context else {}
        payload.update(
            {
                "operation": operation,
                "error_type": error_type,
                "error_message": error_message,
            }
        )
        return self.emit_event(
            f"{operation} failed with {error_type}: {error_message}",
            level=logging.ERROR,
            channel=channel or self.default_channel,
            event_type="error",
            status="failed",
            legacy_category="error",
            **payload,
        )
