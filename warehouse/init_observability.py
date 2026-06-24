"""Initialize the BQuant observability database and directory layout."""

from __future__ import annotations

from pathlib import Path

import yaml

from warehouse.observability_connection import ensure_observability_initialized


REPO_ROOT = Path(__file__).resolve().parent.parent
OBSERVABILITY_CONFIG_PATH = REPO_ROOT / "configs" / "observability.yaml"


def _load_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def ensure_observability_directories() -> None:
    config = _load_yaml(OBSERVABILITY_CONFIG_PATH)
    logs_cfg = config.get("logs", {})
    directories = [
        logs_cfg.get("base_dir", "logs/observability"),
        logs_cfg.get("jsonl_dir", "logs/observability/jsonl"),
        logs_cfg.get("parquet_dir", "logs/observability/parquet"),
        logs_cfg.get("dead_letter_dir", "logs/observability/dead_letter"),
    ]
    for channel_path in logs_cfg.get("channels", {}).values():
        directories.append(channel_path)

    for relative in directories:
        (REPO_ROOT / relative).mkdir(parents=True, exist_ok=True)


def initialize_observability() -> str:
    ensure_observability_directories()
    return ensure_observability_initialized()


if __name__ == "__main__":
    print(initialize_observability())
