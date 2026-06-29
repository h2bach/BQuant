"""CLI entrypoint for the BQuant deterministic agent cycle."""

from __future__ import annotations

import argparse
from datetime import datetime
from typing import Any

from agents.orchestrator import run_agent_cycle


def parse_args() -> argparse.Namespace:
    """Parse command-line options for the agent cycle.

    Returns:
        Namespace containing trigger metadata, optional date, and row limit.
    """
    parser = argparse.ArgumentParser(description="Run BQuant deterministic recommendation agents.")
    parser.add_argument("--as-of-date", help="Optional trading date in YYYY-MM-DD format.")
    parser.add_argument("--trigger-type", default="manual", choices=["manual", "scheduled", "recovery"])
    parser.add_argument("--limit", type=int, help="Optional row limit for smoke tests.")
    return parser.parse_args()


def _parse_date(value: str | None) -> datetime.date | None:
    """Parse optional ISO date input.

    Args:
        value: Optional `YYYY-MM-DD` string.

    Returns:
        Parsed date or `None`.
    """
    if value is None:
        return None
    return datetime.fromisoformat(value).date()


def main() -> dict[str, Any]:
    """Run the CLI agent cycle and return its summary."""
    args = parse_args()
    return run_agent_cycle(
        as_of_date=_parse_date(args.as_of_date),
        trigger_type=args.trigger_type,
        limit=args.limit,
    )


if __name__ == "__main__":
    main()
