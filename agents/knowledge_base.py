"""BQuant knowledge ingestion and lightweight retrieval for live LLM chat."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from warehouse.duckdb_connection import get_connection


REPO_ROOT = Path(__file__).resolve().parent.parent
MAX_CHUNK_CHARS = 2400
CHUNK_OVERLAP_CHARS = 240

INCLUDE_PATTERNS = [
    "README.md",
    "architectures/**/*.md",
    "skills/**/*.md",
    "plans/**/*.md",
    "agent_build_reports/**/*.md",
    "configs/*.yaml",
    "warehouse/*schema*.sql",
    "transformations/dbt/README.md",
    "transformations/dbt/dbt_project.yml",
    "transformations/dbt/models/**/*.sql",
    "transformations/dbt/models/**/*.yml",
    "agents/**/*.py",
    "agents/llm_playbooks/**/*.md",
    "agents/llm_playbooks/**/*.yaml",
    "apps/web/**/*.py",
    "pipelines/**/*.py",
    "data_ingestion/**/*.py",
    "warehouse/**/*.py",
    "utils/**/*.py",
]

EXCLUDED_PARTS = {
    ".git",
    ".pytest_cache",
    "__pycache__",
    "target",
    "logs",
    "data",
    ".mypy_cache",
    ".ruff_cache",
}

STOPWORDS = {
    "the",
    "and",
    "for",
    "with",
    "from",
    "this",
    "that",
    "are",
    "was",
    "were",
    "can",
    "will",
    "should",
    "cua",
    "cho",
    "voi",
    "nhung",
    "trong",
    "hien",
    "tai",
    "duoc",
    "khong",
}


@dataclass(frozen=True)
class KnowledgeChunk:
    """One retrieved knowledge chunk for a live LLM prompt.

    Attributes:
        chunk_id: Stable chunk identifier.
        source_path: Repository-relative source path.
        source_type: Coarse source category such as docs, config, dbt, or code.
        title: Human-readable document title.
        content: Chunk text.
        score: Retrieval relevance score.
    """

    chunk_id: str
    source_path: str
    source_type: str
    title: str
    content: str
    score: float


def ensure_knowledge_tables() -> None:
    """Create knowledge-ingestion tables and views if schema init has not run.

    Side Effects:
        Executes idempotent DDL in the primary DuckDB warehouse.
    """
    with get_connection(read_only=False) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS agent_knowledge_documents (
                doc_id VARCHAR PRIMARY KEY,
                source_path VARCHAR NOT NULL,
                source_type VARCHAR NOT NULL,
                title VARCHAR NOT NULL,
                content_hash VARCHAR NOT NULL,
                char_count INTEGER NOT NULL,
                ingested_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS agent_knowledge_chunks (
                chunk_id VARCHAR PRIMARY KEY,
                doc_id VARCHAR NOT NULL,
                chunk_index INTEGER NOT NULL,
                source_path VARCHAR NOT NULL,
                source_type VARCHAR NOT NULL,
                title VARCHAR NOT NULL,
                content VARCHAR NOT NULL,
                content_hash VARCHAR NOT NULL,
                char_count INTEGER NOT NULL,
                ingested_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_agent_knowledge_chunks_doc
            ON agent_knowledge_chunks(doc_id)
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_agent_knowledge_chunks_source
            ON agent_knowledge_chunks(source_type, source_path)
            """
        )
        conn.execute(
            """
            CREATE OR REPLACE VIEW v_agent_knowledge_inventory AS
            SELECT
                documents.source_type,
                count(DISTINCT documents.doc_id) AS document_count,
                count(chunks.chunk_id) AS chunk_count,
                sum(documents.char_count) AS total_document_chars,
                max(documents.ingested_at) AS last_ingested_at
            FROM agent_knowledge_documents documents
            LEFT JOIN agent_knowledge_chunks chunks
              ON documents.doc_id = chunks.doc_id
            GROUP BY documents.source_type
            ORDER BY documents.source_type
            """
        )


def _content_hash(content: str) -> str:
    """Return a stable SHA-256 hash for text content."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _doc_id(relative_path: str) -> str:
    """Return a stable document id for a repository-relative path."""
    return hashlib.sha1(relative_path.encode("utf-8")).hexdigest()


def _source_type(path: Path) -> str:
    """Classify a repository path into a knowledge source type."""
    parts = set(path.parts)
    if "architectures" in parts:
        return "architecture"
    if "plans" in parts:
        return "plan"
    if "agent_build_reports" in parts:
        return "build_report"
    if "llm_playbooks" in parts and "skills" in parts:
        return "llm_skill"
    if "llm_playbooks" in parts and "references" in parts:
        return "llm_reference"
    if "llm_playbooks" in parts:
        return "llm_plan"
    if "skills" in parts:
        return "skill"
    if "configs" in parts:
        return "config"
    if "transformations" in parts:
        return "dbt"
    if "warehouse" in parts and path.suffix == ".sql":
        return "warehouse_schema"
    if path.suffix == ".py":
        return "source_code"
    return "project_doc"


def _title_for(path: Path, content: str) -> str:
    """Resolve a concise title for a knowledge document."""
    for line in content.splitlines()[:30]:
        stripped = line.strip()
        if stripped.startswith("#"):
            return stripped.lstrip("#").strip() or path.name
        if stripped.startswith('"""') and len(stripped) > 6:
            return stripped.strip('"').strip() or path.name
    return path.name


def _is_allowed_file(path: Path) -> bool:
    """Return whether a path should be ingested into the BQuant knowledge base."""
    try:
        relative = path.relative_to(REPO_ROOT)
    except ValueError:
        return False
    if any(part in EXCLUDED_PARTS for part in relative.parts):
        return False
    if path.suffix.lower() not in {".md", ".yaml", ".yml", ".sql", ".py"}:
        return False
    return path.is_file()


def iter_knowledge_files() -> list[Path]:
    """Resolve all repository files included in the knowledge-ingestion scope.

    Returns:
        Sorted unique absolute paths.
    """
    paths: set[Path] = set()
    for pattern in INCLUDE_PATTERNS:
        paths.update(path.resolve() for path in REPO_ROOT.glob(pattern) if _is_allowed_file(path.resolve()))
    return sorted(paths, key=lambda path: str(path.relative_to(REPO_ROOT)))


def _read_text(path: Path) -> str:
    """Read a source file as UTF-8 text with replacement for invalid bytes."""
    return path.read_text(encoding="utf-8", errors="replace")


def chunk_text(content: str, *, max_chars: int = MAX_CHUNK_CHARS) -> list[str]:
    """Split long text into prompt-sized chunks with small overlap.

    Args:
        content: Full document text.
        max_chars: Maximum characters per chunk.

    Returns:
        Ordered text chunks. Empty documents return an empty list.
    """
    normalized = content.replace("\r\n", "\n").strip()
    if not normalized:
        return []
    if len(normalized) <= max_chars:
        return [normalized]

    chunks: list[str] = []
    start = 0
    while start < len(normalized):
        end = min(start + max_chars, len(normalized))
        split_at = normalized.rfind("\n\n", start, end)
        if split_at <= start + max_chars // 2:
            split_at = normalized.rfind("\n", start, end)
        if split_at <= start + max_chars // 2:
            split_at = end
        chunk = normalized[start:split_at].strip()
        if chunk:
            chunks.append(chunk)
        if split_at >= len(normalized):
            break
        start = max(0, split_at - CHUNK_OVERLAP_CHARS)
    return chunks


def ingest_knowledge_base() -> dict[str, Any]:
    """Ingest BQuant project knowledge into DuckDB document/chunk tables.

    Returns:
        Summary dictionary with document and chunk counts.
    """
    ensure_knowledge_tables()
    paths = iter_knowledge_files()
    ingested_at = datetime.now()
    document_rows: list[list[Any]] = []
    chunk_rows: list[list[Any]] = []

    for path in paths:
        relative_path = str(path.relative_to(REPO_ROOT))
        content = _read_text(path)
        doc_id = _doc_id(relative_path)
        content_hash = _content_hash(content)
        title = _title_for(path, content)
        source_type = _source_type(path.relative_to(REPO_ROOT))
        document_rows.append(
            [
                doc_id,
                relative_path,
                source_type,
                title,
                content_hash,
                len(content),
                ingested_at,
            ]
        )
        for index, chunk in enumerate(chunk_text(content)):
            chunk_hash = _content_hash(f"{relative_path}:{index}:{chunk}")
            chunk_id = hashlib.sha1(f"{doc_id}:{index}:{chunk_hash}".encode("utf-8")).hexdigest()
            chunk_rows.append(
                [
                    chunk_id,
                    doc_id,
                    index,
                    relative_path,
                    source_type,
                    title,
                    chunk,
                    chunk_hash,
                    len(chunk),
                    ingested_at,
                ]
            )

    with get_connection(read_only=False) as conn:
        conn.execute("DELETE FROM agent_knowledge_chunks")
        conn.execute("DELETE FROM agent_knowledge_documents")
        if document_rows:
            conn.executemany(
                """
                INSERT INTO agent_knowledge_documents (
                    doc_id,
                    source_path,
                    source_type,
                    title,
                    content_hash,
                    char_count,
                    ingested_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                document_rows,
            )
        if chunk_rows:
            conn.executemany(
                """
                INSERT INTO agent_knowledge_chunks (
                    chunk_id,
                    doc_id,
                    chunk_index,
                    source_path,
                    source_type,
                    title,
                    content,
                    content_hash,
                    char_count,
                    ingested_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                chunk_rows,
            )

    return {
        "status": "success",
        "document_count": len(document_rows),
        "chunk_count": len(chunk_rows),
        "ingested_at": ingested_at.isoformat(),
    }


def _terms(text: str) -> set[str]:
    """Tokenize a query or chunk into normalized retrieval terms."""
    raw_terms = re.findall(r"[\w][\w\-.]{1,}", text.lower(), flags=re.UNICODE)
    return {term for term in raw_terms if term not in STOPWORDS and len(term) >= 2}


def retrieve_knowledge_chunks(
    question: str,
    *,
    limit: int = 8,
    max_total_chars: int = 12000,
) -> list[KnowledgeChunk]:
    """Retrieve relevant BQuant knowledge chunks for a user question.

    Args:
        question: User question from live chat.
        limit: Maximum chunks to return.
        max_total_chars: Character budget for returned chunk content.

    Returns:
        Ranked chunks constrained by the character budget.
    """
    ensure_knowledge_tables()
    query_terms = _terms(question)
    if not query_terms:
        query_terms = {"bquant", "system", "data", "agent"}

    with get_connection(read_only=True) as conn:
        frame = conn.execute(
            """
            SELECT chunk_id,
                   source_path,
                   source_type,
                   title,
                   content,
                   char_count
            FROM agent_knowledge_chunks
            """
        ).df()
    if frame.empty:
        return []

    scored: list[KnowledgeChunk] = []
    for _, row in frame.iterrows():
        content = str(row["content"])
        source_path = str(row["source_path"])
        source_type = str(row["source_type"])
        haystack_terms = _terms(f"{source_path} {source_type} {row['title']} {content[:4000]}")
        overlap = query_terms.intersection(haystack_terms)
        if not overlap:
            continue
        path_boost = 1.5 if any(term in source_path.lower() for term in query_terms) else 0.0
        type_boost = 1.0 if source_type in {"architecture", "project_doc", "warehouse_schema", "dbt"} else 0.0
        if source_type in {"llm_plan", "llm_skill", "llm_reference"}:
            type_boost += 1.5
        score = float(len(overlap) * 2.0 + path_boost + type_boost)
        scored.append(
            KnowledgeChunk(
                chunk_id=str(row["chunk_id"]),
                source_path=source_path,
                source_type=source_type,
                title=str(row["title"]),
                content=content,
                score=score,
            )
        )

    scored.sort(key=lambda chunk: (-chunk.score, chunk.source_path, chunk.chunk_id))
    selected: list[KnowledgeChunk] = []
    total_chars = 0
    for chunk in scored:
        if len(selected) >= limit:
            break
        next_total = total_chars + len(chunk.content)
        if selected and next_total > max_total_chars:
            continue
        selected.append(chunk)
        total_chars = next_total
    return selected


def render_knowledge_context(question: str, *, limit: int = 8, max_total_chars: int = 12000) -> str:
    """Render retrieved knowledge chunks into a prompt-ready Markdown block.

    Args:
        question: User question from live chat.
        limit: Maximum chunks to include.
        max_total_chars: Character budget for chunk content.

    Returns:
        Markdown context block with source labels. Returns a short unavailable
        message when the knowledge base has not been ingested yet.
    """
    chunks = retrieve_knowledge_chunks(question, limit=limit, max_total_chars=max_total_chars)
    if not chunks:
        return "No persisted BQuant knowledge chunks were retrieved. Use pipelines.ingest_agent_knowledge first."
    lines = ["# Retrieved BQuant Knowledge"]
    for index, chunk in enumerate(chunks, start=1):
        lines.extend(
            [
                "",
                f"## Source {index}: {chunk.source_path}",
                f"- Type: {chunk.source_type}",
                f"- Title: {chunk.title}",
                f"- Retrieval score: {chunk.score:.2f}",
                "",
                chunk.content,
            ]
        )
    return "\n".join(lines)


def load_knowledge_inventory() -> pd.DataFrame:
    """Load inventory counts for the ingested BQuant knowledge base."""
    ensure_knowledge_tables()
    with get_connection(read_only=True) as conn:
        return conn.execute("SELECT * FROM v_agent_knowledge_inventory").df()
