"""SQLite 到 PostgreSQL 的状态迁移工具。"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import sqlite3
from typing import Literal


class StateMigrationError(RuntimeError):
    """状态迁移错误。"""


@dataclass(frozen=True, slots=True)
class StateMigrationReport:
    """迁移 dry-run 或 execute 的计数报告。"""

    mode: Literal["dry-run", "execute"]
    rag_chunks: int
    rag_files: int
    memories: int
    memory_curation_decisions: int
    maintenance_proposals: int
    checkpoint_migrated: bool
    notes: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        """转换为 CLI JSON 输出。"""

        return {
            "mode": self.mode,
            "rag_chunks": self.rag_chunks,
            "rag_files": self.rag_files,
            "memories": self.memories,
            "memory_curation_decisions": self.memory_curation_decisions,
            "maintenance_proposals": self.maintenance_proposals,
            "checkpoint_migrated": self.checkpoint_migrated,
            "notes": self.notes,
        }


def _connect_sqlite(path: Path) -> sqlite3.Connection | None:
    if not path.is_file():
        return None
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    return connection


def _count(connection: sqlite3.Connection | None, table: str) -> int:
    if connection is None:
        return 0
    try:
        row = connection.execute(f"SELECT COUNT(*) AS total FROM {table}").fetchone()
    except sqlite3.Error:
        return 0
    return int(row["total"])


def _vector_literal(value: str) -> str | None:
    """把 SQLite JSON 向量转换为 pgvector 文本字面量。"""

    try:
        values = [float(item) for item in json.loads(value)]
    except Exception:
        return None
    if not values:
        return None
    return "[" + ",".join(f"{item:.12g}" for item in values) + "]"


def _require_psycopg():
    try:
        import psycopg
    except ImportError as exc:
        raise StateMigrationError("执行 PostgreSQL 迁移需要安装 repo-agent[postgres]") from exc
    return psycopg


def migrate_state(
    *,
    sqlite_state_dir: str | Path,
    postgres_dsn: str,
    execute: bool,
) -> StateMigrationReport:
    """把可可靠映射的 SQLite 状态复制到 PostgreSQL。"""

    source = Path(sqlite_state_dir).expanduser().resolve()
    rag = _connect_sqlite(source / "rag.sqlite3")
    memory = _connect_sqlite(source / "memory.sqlite3")
    proposal = _connect_sqlite(source / "maintenance.sqlite3")
    try:
        report = StateMigrationReport(
            mode="execute" if execute else "dry-run",
            rag_chunks=_count(rag, "rag_chunks"),
            rag_files=_count(rag, "rag_documents"),
            memories=_count(memory, "memories"),
            memory_curation_decisions=_count(memory, "memory_curation_decisions"),
            maintenance_proposals=_count(proposal, "maintenance_proposals"),
            checkpoint_migrated=False,
            notes=(
                "Checkpoint 不做猜测转换；请让旧线程在 SQLite 上完成，或在 PostgreSQL 上重新开始。",
                "旧维护候选是 JSON 制品文件，不在 migrate-state 中转换为数据库 proposal。",
                "迁移不会删除 SQLite 源文件，也不会输出源码或 Memory 正文。",
            ),
        )
        if not execute:
            return report
        psycopg = _require_psycopg()
        with psycopg.connect(postgres_dsn) as target:
            with target.transaction():
                with target.cursor() as cursor:
                    if rag is not None:
                        for row in rag.execute("SELECT * FROM rag_documents"):
                            cursor.execute(
                                """
                                INSERT INTO repository_files (
                                    project_id, repo_revision, path, file_hash, chunk_count
                                ) VALUES (%s, %s, %s, %s, %s)
                                ON CONFLICT(project_id, path) DO UPDATE SET
                                    repo_revision = excluded.repo_revision,
                                    file_hash = excluded.file_hash,
                                    chunk_count = excluded.chunk_count,
                                    updated_at = now()
                                """,
                                (
                                    row["project_id"],
                                    row["repo_revision"],
                                    row["path"],
                                    row["file_hash"],
                                    row["chunk_count"],
                                ),
                            )
                        for row in rag.execute("SELECT * FROM rag_index_state"):
                            cursor.execute(
                                """
                                INSERT INTO repository_index_state (
                                    project_id, repo_revision, embedding_model,
                                    embedding_dimensions, updated_at
                                ) VALUES (%s, %s, %s, %s, %s)
                                ON CONFLICT(project_id) DO UPDATE SET
                                    repo_revision = excluded.repo_revision,
                                    embedding_model = excluded.embedding_model,
                                    embedding_dimensions = excluded.embedding_dimensions,
                                    updated_at = excluded.updated_at
                                """,
                                (
                                    row["project_id"],
                                    row["repo_revision"],
                                    row["embedding_model"],
                                    row["embedding_dimensions"],
                                    row["updated_at"],
                                ),
                            )
                        for row in rag.execute("SELECT * FROM rag_chunks"):
                            vector = _vector_literal(row["embedding_json"])
                            cursor.execute(
                                """
                                INSERT INTO repository_chunks (
                                    chunk_id, project_id, repo_revision, path,
                                    start_line, end_line, kind, language, symbol,
                                    content, content_hash, embedding_model,
                                    embedding_dimensions, embedding,
                                    search_document, token_text, heading_path_json
                                ) VALUES (
                                    %s, %s,
                                    (SELECT repo_revision FROM repository_index_state WHERE project_id = %s),
                                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                                    %s::vector, to_tsvector('simple', %s), %s, %s
                                )
                                ON CONFLICT(chunk_id) DO UPDATE SET
                                    content_hash = excluded.content_hash,
                                    embedding = excluded.embedding,
                                    search_document = excluded.search_document,
                                    updated_at = now()
                                """,
                                (
                                    row["chunk_id"],
                                    row["project_id"],
                                    row["project_id"],
                                    row["path"],
                                    row["start_line"],
                                    row["end_line"],
                                    row["kind"],
                                    row["language"],
                                    row["symbol"],
                                    row["content"],
                                    row["content_hash"],
                                    row["embedding_model"],
                                    row["embedding_dimensions"],
                                    vector,
                                    f"{row['path']}\n{row['symbol'] or ''}\n{row['content']}",
                                    f"{row['path']} {row['symbol'] or ''}",
                                    row["heading_path_json"],
                                ),
                            )
                    if memory is not None:
                        for row in memory.execute("SELECT * FROM memories"):
                            cursor.execute(
                                """
                                INSERT INTO memories (
                                    memory_id, project_id, memory_key, memory_type,
                                    content, claim_status, importance, scope,
                                    repo_revision, source, source_id, evidence_json,
                                    tags_json, status, supersedes_memory_id,
                                    expires_at, embedding_model, embedding_dimensions,
                                    search_document, token_text, created_at, updated_at
                                ) VALUES (
                                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                                    %s, %s, %s, %s, %s, %s, %s,
                                    to_tsvector('simple', %s), %s, %s, %s
                                )
                                ON CONFLICT(memory_id) DO NOTHING
                                """,
                                (
                                    row["memory_id"],
                                    row["project_id"],
                                    row["memory_key"],
                                    row["memory_type"],
                                    row["content"],
                                    row["claim_status"],
                                    row["importance"],
                                    row["scope"],
                                    row["repo_revision"],
                                    row["source"],
                                    row["source_id"],
                                    row["evidence_json"],
                                    row["tags_json"],
                                    row["status"],
                                    row["supersedes_memory_id"],
                                    row["expires_at"],
                                    row["embedding_model"],
                                    row["embedding_dimensions"],
                                    row["content"],
                                    "",
                                    row["created_at"],
                                    row["updated_at"],
                                ),
                            )
                        for row in memory.execute("SELECT * FROM memory_curation_decisions"):
                            cursor.execute(
                                """
                                INSERT INTO memory_curation_decisions (
                                    project_id, candidate_id, candidate_json,
                                    action, reason, matched_memory_id,
                                    result_memory_id, decided_by, created_at, updated_at
                                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                                ON CONFLICT(project_id, candidate_id) DO NOTHING
                                """,
                                (
                                    row["project_id"],
                                    row["candidate_id"],
                                    row["candidate_json"],
                                    row["action"],
                                    row["reason"],
                                    row["matched_memory_id"],
                                    row["result_memory_id"],
                                    row["decided_by"],
                                    row["created_at"],
                                    row["updated_at"],
                                ),
                            )
        return report
    finally:
        for connection in (rag, memory, proposal):
            if connection is not None:
                connection.close()
