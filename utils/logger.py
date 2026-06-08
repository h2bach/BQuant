"""Centralized logging utilities for BQuant."""

from __future__ import annotations

import json
import logging
import sys
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

import yaml


REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = REPO_ROOT / "configs" / "bquant.yaml"


def _load_logging_config() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        return {
            "base_dir": "logs",
            "data_dir": "logs/data",
            "pipeline_dir": "logs/pipeline",
            "error_dir": "logs/errors",
            "format": "jsonl",
            "level": "INFO",
        }

    with CONFIG_PATH.open("r", encoding="utf-8") as handle:
        config = yaml.safe_load(handle) or {}

    return config.get("logging", {})


@dataclass(frozen=True)
class LogPaths:
    base_dir: Path
    data_dir: Path
    pipeline_dir: Path
    error_dir: Path


class BQuantLogger:
    """Structured logger for pipeline and data operations."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.config = _load_logging_config()
        self.paths = self._build_paths()
        self._ensure_directories()
        self.logger = self._setup_logger()

    def _build_paths(self) -> LogPaths:
        base_dir = REPO_ROOT / self.config.get("base_dir", "logs")
        data_dir = REPO_ROOT / self.config.get("data_dir", "logs/data")
        pipeline_dir = REPO_ROOT / self.config.get("pipeline_dir", "logs/pipeline")
        error_dir = REPO_ROOT / self.config.get("error_dir", "logs/errors")
        return LogPaths(
            base_dir=base_dir,
            data_dir=data_dir,
            pipeline_dir=pipeline_dir,
            error_dir=error_dir,
        )

    def _ensure_directories(self) -> None:
        directories = [
            self.paths.base_dir,
            self.paths.data_dir,
            self.paths.pipeline_dir,
            self.paths.error_dir,
            self.paths.data_dir / "ingestion",
            self.paths.data_dir / "features",
            self.paths.data_dir / "backtesting",
            self.paths.data_dir / "signals",
        ]
        for directory in directories:
            directory.mkdir(parents=True, exist_ok=True)

    def _setup_logger(self) -> logging.Logger:
        logger = logging.getLogger(f"bquant.{self.name}")
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

    def _dated_path(self, directory: Path, extension: str) -> Path:
        date_str = datetime.now().strftime("%Y%m%d")
        return directory / f"{self.name}_{date_str}.{extension}"

    def _json_default(self, value: Any) -> str:
        if isinstance(value, (datetime, date)):
            return value.isoformat()
        if isinstance(value, Path):
            return str(value)
        return str(value)

    def _write_jsonl(self, directory: Path, payload: dict[str, Any]) -> Path:
        log_path = self._dated_path(directory, "jsonl")
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=True, default=self._json_default) + "\n")
        return log_path

    def _write_text(self, directory: Path, message: str) -> Path:
        log_path = self._dated_path(directory, "log")
        timestamp = datetime.now().isoformat()
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(f"{timestamp} {message}\n")
        return log_path

    def _record(
        self,
        *,
        level: int,
        category: str,
        message: str,
        payload: dict[str, Any],
        subcategory: str | None = None,
    ) -> None:
        if category == "pipeline":
            directory = self.paths.pipeline_dir
        elif category == "error":
            directory = self.paths.error_dir
        elif category == "data":
            directory = self.paths.data_dir / subcategory if subcategory else self.paths.data_dir
            directory.mkdir(parents=True, exist_ok=True)
        else:
            raise ValueError(f"Unsupported log category: {category}")

        enriched_payload = {
            "timestamp": datetime.now().isoformat(),
            "logger_name": self.name,
            **payload,
        }
        self._write_jsonl(directory, enriched_payload)
        self._write_text(directory, message)
        self.logger.log(level, message)

    def info(self, message: str, **payload: Any) -> None:
        self._record(level=logging.INFO, category="pipeline", message=message, payload=payload)

    def warning(self, message: str, **payload: Any) -> None:
        self._record(level=logging.WARNING, category="pipeline", message=message, payload=payload)

    def log_data_event(self, subcategory: str, message: str, **payload: Any) -> None:
        self._record(
            level=logging.INFO,
            category="data",
            subcategory=subcategory,
            message=message,
            payload=payload,
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
    ) -> None:
        message = (
            f"Pipeline {pipeline_name} finished with status={status}; "
            f"completed={len(steps_completed)} failed={len(steps_failed)}"
        )
        self._record(
            level=logging.INFO if status == "success" else logging.ERROR,
            category="pipeline",
            message=message,
            payload={
                "operation": "pipeline_run",
                "pipeline_name": pipeline_name,
                "start_time": start_time,
                "end_time": end_time,
                "status": status,
                "steps_completed": steps_completed,
                "steps_failed": steps_failed,
                "error_message": error_message,
                **extra,
            },
        )

    def log_error(
        self,
        operation: str,
        error_type: str,
        error_message: str,
        *,
        context: dict[str, Any] | None = None,
    ) -> None:
        self._record(
            level=logging.ERROR,
            category="error",
            message=f"{operation} failed with {error_type}: {error_message}",
            payload={
                "operation": operation,
                "error_type": error_type,
                "error_message": error_message,
                "context": context or {},
            },
        )
