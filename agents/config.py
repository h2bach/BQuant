"""Configuration helpers for BQuant recommendation agents."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


REPO_ROOT = Path(__file__).resolve().parent.parent
AGENT_CONFIG_PATH = REPO_ROOT / "configs" / "agents.yaml"


def load_agent_config(path: Path | None = None) -> dict[str, Any]:
    """Load agent runtime configuration.

    Args:
        path: Optional YAML path. Defaults to `configs/agents.yaml`.

    Returns:
        Parsed configuration mapping. Missing files return an empty mapping so
        callers can still rely on built-in defaults.
    """
    config_path = path or AGENT_CONFIG_PATH
    if not config_path.exists():
        return {}
    with config_path.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def runtime_config(config: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return the `runtime` config block.

    Args:
        config: Optional preloaded agent configuration.

    Returns:
        Runtime settings with table names and backend mode.
    """
    return (config or load_agent_config()).get("runtime", {})


def threshold_config(config: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return scoring threshold settings.

    Args:
        config: Optional preloaded agent configuration.

    Returns:
        Threshold settings for stale data, score cutoffs, and confidence.
    """
    return (config or load_agent_config()).get("thresholds", {})


def optimizer_config(config: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return portfolio optimizer settings.

    Args:
        config: Optional preloaded agent configuration.

    Returns:
        Settings for position count, weight bounds, and QAOA enablement.
    """
    return (config or load_agent_config()).get("agents", {}).get("portfolio_optimizer_agent", {})
