"""CLI smoke test for BQuant live LLM chat."""

from __future__ import annotations

import argparse

from agents.system_analysis import answer_live_system_question


def parse_args() -> argparse.Namespace:
    """Parse CLI arguments for the live LLM chat smoke test.

    Returns:
        Parsed namespace containing the user question.
    """
    parser = argparse.ArgumentParser(description="Ask BQuant's live local LLM assistant.")
    parser.add_argument("question", nargs="+", help="Question to answer with fresh BQuant context.")
    return parser.parse_args()


def main() -> None:
    """Run one live LLM answer and print the response metadata."""
    args = parse_args()
    question = " ".join(args.question)
    result = answer_live_system_question(question)
    print(f"source={result['answer_source']} model={result.get('model')} latency={result.get('latency_seconds')}")
    if result.get("error_message"):
        print(f"error={result['error_message']}")
    print()
    print(result["answer_markdown"])


if __name__ == "__main__":
    main()
