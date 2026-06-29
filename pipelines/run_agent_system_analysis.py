"""CLI entrypoint for BQuant system-analysis agents."""

from __future__ import annotations

import argparse
from typing import Any

from agents.system_analysis import run_system_analysis


def parse_args() -> argparse.Namespace:
    """Parse command-line options for the system-analysis agent run.

    Returns:
        Namespace containing trigger metadata, optional question, and whether
        to refresh deterministic recommendations before reporting.
    """
    parser = argparse.ArgumentParser(description="Run BQuant data, TA, and portfolio analysis agents.")
    parser.add_argument("--trigger-type", default="manual", choices=["manual", "scheduled", "recovery"])
    parser.add_argument("--question", help="Optional question answered from the generated report.")
    parser.add_argument(
        "--refresh-recommendations",
        action="store_true",
        help="Run the deterministic recommendation cycle before portfolio summarization.",
    )
    return parser.parse_args()


def main() -> dict[str, Any]:
    """Run the system-analysis agents and return a summary dictionary."""
    args = parse_args()
    return run_system_analysis(
        trigger_type=args.trigger_type,
        question=args.question,
        refresh_recommendations=args.refresh_recommendations,
    )


if __name__ == "__main__":
    main()
