"""Ingest BQuant project knowledge for live LLM/RAG chat."""

from __future__ import annotations

from agents.knowledge_base import ingest_knowledge_base, load_knowledge_inventory
from utils.logger import BQuantLogger


PIPELINE_NAME = "ingest_agent_knowledge"


def main() -> None:
    """Run BQuant knowledge ingestion and print an inventory summary."""
    logger = BQuantLogger(PIPELINE_NAME, component="agent", subcomponent=PIPELINE_NAME, default_channel="pipeline")
    try:
        result = ingest_knowledge_base()
        inventory = load_knowledge_inventory()
        logger.log_pipeline_run(
            pipeline_name=PIPELINE_NAME,
            start_time=result["ingested_at"],
            end_time=result["ingested_at"],
            status="success",
            steps_completed=["ingest_knowledge_base"],
            steps_failed=[],
            document_count=result["document_count"],
            chunk_count=result["chunk_count"],
        )
        print(result)
        if not inventory.empty:
            print(inventory.to_string(index=False))
    except Exception as exc:
        logger.log_error(
            PIPELINE_NAME,
            type(exc).__name__,
            str(exc),
            context={},
            channel="pipeline",
        )
        logger.log_pipeline_run(
            pipeline_name=PIPELINE_NAME,
            start_time="",
            end_time="",
            status="failed",
            steps_completed=[],
            steps_failed=["ingest_knowledge_base"],
            error_message=str(exc),
        )
        raise


if __name__ == "__main__":
    main()
