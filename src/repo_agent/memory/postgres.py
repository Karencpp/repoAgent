"""PostgreSQL/pgvector 长期记忆后端。"""

from __future__ import annotations

from datetime import datetime, timezone
import json
from uuid import uuid4

from repo_agent.postgres_vectors import hnsw_vector_type
from repo_agent.projects import ProjectContext
from repo_agent.rag.embeddings import EmbeddingClient

from .models import (
    MemoryMaintenanceReport,
    MemoryRecord,
    MemorySearchRequest,
    MemorySearchResult,
    MemoryWrite,
)
from .store import MemoryEmbeddingMismatchError, MemoryNotFoundError


class PostgresMemoryError(RuntimeError):
    """PostgreSQL Memory 后端错误。"""


def _require_psycopg():
    """惰性导入 psycopg，避免默认 SQLite 模式依赖 PostgreSQL 包。"""

    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError as exc:
        raise PostgresMemoryError("PostgreSQL 后端需要安装可选依赖：repo-agent[postgres]") from exc
    return psycopg, dict_row


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _vector_literal(values: tuple[float, ...]) -> str:
    return "[" + ",".join(f"{value:.12g}" for value in values) + "]"


class PostgresMemoryStore:
    """使用 PostgreSQL 约束、FTS 和 pgvector 的长期记忆 Store。"""

    def __init__(self, dsn: str, embedding_client: EmbeddingClient) -> None:
        psycopg, dict_row = _require_psycopg()
        self.embedding_client = embedding_client
        self._vector_type = hnsw_vector_type(embedding_client.dimensions)
        self._connection = psycopg.connect(dsn, row_factory=dict_row, autocommit=True)

    def close(self) -> None:
        """关闭 PostgreSQL 连接。"""

        self._connection.close()

    def _validate_embedding_state(self, project_id: str, *, allow_empty: bool) -> None:
        with self._connection.cursor() as cursor:
            row = cursor.execute(
                """
                SELECT embedding_model, embedding_dimensions
                FROM memory_embedding_state WHERE project_id = %s
                """,
                (project_id,),
            ).fetchone()
        if row is None:
            if allow_empty:
                return
            raise PostgresMemoryError(f"项目尚无长期记忆：{project_id}")
        if (
            row["embedding_model"] != self.embedding_client.model_id
            or row["embedding_dimensions"] != self.embedding_client.dimensions
        ):
            raise MemoryEmbeddingMismatchError("长期记忆向量空间与当前客户端不一致")

    @staticmethod
    def _row_to_record(row) -> MemoryRecord:
        return MemoryRecord(
            memory_id=str(row["memory_id"]),
            project_id=str(row["project_id"]),
            memory_key=row["memory_key"],
            memory_type=str(row["memory_type"]),
            content=str(row["content"]),
            claim_status=str(row["claim_status"]),
            importance=float(row["importance"]),
            scope=str(row["scope"]),
            repo_revision=row["repo_revision"],
            source=str(row["source"]),
            source_id=str(row["source_id"]),
            evidence=tuple(json.loads(row["evidence_json"] or "[]")),
            tags=tuple(json.loads(row["tags_json"] or "[]")),
            status=str(row["status"]),
            supersedes_memory_id=row["supersedes_memory_id"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            expires_at=row["expires_at"],
            embedding_model=str(row["embedding_model"]),
            embedding_dimensions=int(row["embedding_dimensions"]),
        )

    def _embed_memory(self, memory: MemoryWrite) -> tuple[float, ...]:
        vectors = self.embedding_client.embed_texts((memory.content,))
        if len(vectors) != 1 or len(vectors[0]) != self.embedding_client.dimensions:
            raise MemoryEmbeddingMismatchError("记忆 Embedding 数量或维度错误")
        return tuple(vectors[0])

    def _insert(
        self,
        cursor,
        context: ProjectContext,
        memory: MemoryWrite,
        embedding: tuple[float, ...],
        *,
        memory_key: str | None,
        supersedes_memory_id: str | None = None,
    ) -> MemoryRecord:
        memory_id = f"memory-{uuid4().hex}"
        cursor.execute(
            """
            INSERT INTO memories (
                memory_id, project_id, memory_key, memory_type, content,
                claim_status, importance, scope, repo_revision, source, source_id,
                evidence_json, tags_json, status, supersedes_memory_id,
                expires_at, embedding, embedding_model, embedding_dimensions,
                search_document, token_text
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                'active', %s, %s, %s::vector, %s, %s,
                to_tsvector('simple', %s), %s
            )
            RETURNING *
            """,
            (
                memory_id,
                context.project_id,
                memory_key,
                memory.memory_type,
                memory.content,
                memory.claim_status,
                memory.importance,
                memory.scope,
                memory.repo_revision,
                memory.source,
                memory.source_id,
                json.dumps(memory.evidence, ensure_ascii=False),
                json.dumps(memory.tags, ensure_ascii=False),
                supersedes_memory_id,
                memory.expires_at,
                _vector_literal(embedding),
                self.embedding_client.model_id,
                self.embedding_client.dimensions,
                f"{memory.content}\n{' '.join(memory.tags)}",
                " ".join(memory.tags),
            ),
        )
        row = cursor.fetchone()
        cursor.execute(
            """
            INSERT INTO memory_embedding_state (
                project_id, embedding_model, embedding_dimensions, updated_at
            ) VALUES (%s, %s, %s, now())
            ON CONFLICT(project_id) DO UPDATE SET
                embedding_model = excluded.embedding_model,
                embedding_dimensions = excluded.embedding_dimensions,
                updated_at = excluded.updated_at
            """,
            (
                context.project_id,
                self.embedding_client.model_id,
                self.embedding_client.dimensions,
            ),
        )
        return self._row_to_record(row)

    def put(self, context: ProjectContext, memory: MemoryWrite) -> MemoryRecord:
        return self.put_with_key(context, memory, memory_key=None)

    def put_with_key(
        self,
        context: ProjectContext,
        memory: MemoryWrite,
        *,
        memory_key: str | None,
    ) -> MemoryRecord:
        self._validate_embedding_state(context.project_id, allow_empty=True)
        embedding = self._embed_memory(memory)
        with self._connection.transaction():
            with self._connection.cursor() as cursor:
                return self._insert(cursor, context, memory, embedding, memory_key=memory_key)

    def replace(
        self,
        context: ProjectContext,
        old_memory_id: str,
        replacement: MemoryWrite,
    ) -> MemoryRecord:
        self._validate_embedding_state(context.project_id, allow_empty=False)
        embedding = self._embed_memory(replacement)
        with self._connection.transaction():
            with self._connection.cursor() as cursor:
                old = cursor.execute(
                    """
                    SELECT * FROM memories
                    WHERE project_id = %s AND memory_id = %s AND status = 'active'
                    FOR UPDATE
                    """,
                    (context.project_id, old_memory_id),
                ).fetchone()
                if old is None:
                    raise MemoryNotFoundError(f"找不到可替换的记忆：{old_memory_id}")
                cursor.execute(
                    """
                    UPDATE memories SET status = 'superseded', updated_at = now()
                    WHERE project_id = %s AND memory_id = %s
                    """,
                    (context.project_id, old_memory_id),
                )
                return self._insert(
                    cursor,
                    context,
                    replacement,
                    embedding,
                    memory_key=old["memory_key"],
                    supersedes_memory_id=old_memory_id,
                )

    def supersede(
        self,
        context: ProjectContext,
        old_memory_id: str,
        replacement: MemoryWrite,
    ) -> MemoryRecord:
        """兼容 MemoryManager 旧调用名。"""

        return self.replace(context, old_memory_id, replacement)

    def get(self, context: ProjectContext, memory_id: str) -> MemoryRecord:
        with self._connection.cursor() as cursor:
            row = cursor.execute(
                "SELECT * FROM memories WHERE project_id = %s AND memory_id = %s",
                (context.project_id, memory_id),
            ).fetchone()
        if row is None:
            raise MemoryNotFoundError(f"找不到记忆：{memory_id}")
        return self._row_to_record(row)

    def search(
        self,
        context: ProjectContext,
        request: MemorySearchRequest,
    ) -> MemorySearchResult:
        self._validate_embedding_state(context.project_id, allow_empty=False)
        query_vector = self.embedding_client.embed_texts((request.query,))[0]
        with self._connection.cursor() as cursor:
            lexical_rows = cursor.execute(
                """
                SELECT memory_id
                FROM memories
                WHERE project_id = %s AND status = 'active'
                  AND memory_type = ANY(%s) AND claim_status = ANY(%s)
                  AND importance >= %s
                  AND (expires_at IS NULL OR expires_at > now())
                  AND (%s OR scope = 'project' OR repo_revision = %s)
                  AND search_document @@ websearch_to_tsquery('simple', %s)
                ORDER BY ts_rank_cd(search_document, websearch_to_tsquery('simple', %s)) DESC, memory_id
                LIMIT %s
                """,
                (
                    context.project_id,
                    list(request.memory_types),
                    list(request.claim_statuses),
                    request.min_importance,
                    request.include_stale_revisions,
                    context.revision,
                    request.query,
                    request.query,
                    min(80, request.top_k * 4),
                ),
            ).fetchall()
            dense_rows = []
            if any(query_vector):
                dimensions = self.embedding_client.dimensions
                dense_rows = cursor.execute(
                    f"""
                    SELECT memory_id
                    FROM memories
                    WHERE project_id = %s AND status = 'active'
                      AND memory_type = ANY(%s) AND claim_status = ANY(%s)
                      AND importance >= %s
                      AND (expires_at IS NULL OR expires_at > now())
                      AND (%s OR scope = 'project' OR repo_revision = %s)
                      AND embedding_model = %s AND embedding_dimensions = {dimensions}
                    ORDER BY (embedding::{self._vector_type}) <=> %s::{self._vector_type}
                    LIMIT %s
                    """,
                    (
                        context.project_id,
                        list(request.memory_types),
                        list(request.claim_statuses),
                        request.min_importance,
                        request.include_stale_revisions,
                        context.revision,
                        self.embedding_client.model_id,
                        _vector_literal(tuple(query_vector)),
                        min(80, request.top_k * 4),
                    ),
                ).fetchall()
            lexical_ids = [str(row["memory_id"]) for row in lexical_rows]
            dense_ids = [str(row["memory_id"]) for row in dense_rows]
            lexical_ranks = {memory_id: rank for rank, memory_id in enumerate(lexical_ids, 1)}
            dense_ranks = {memory_id: rank for rank, memory_id in enumerate(dense_ids, 1)}
            fused = []
            for memory_id in set(lexical_ranks) | set(dense_ranks):
                score = 0.0
                if memory_id in lexical_ranks:
                    score += 1.0 / (60 + lexical_ranks[memory_id])
                if memory_id in dense_ranks:
                    score += 1.0 / (60 + dense_ranks[memory_id])
                fused.append((memory_id, score))
            fused.sort(key=lambda item: (-item[1], item[0]))
            selected = fused[: request.top_k]
            rows = []
            if selected:
                rows = cursor.execute(
                    "SELECT * FROM memories WHERE memory_id = ANY(%s)",
                    ([memory_id for memory_id, _score in selected],),
                ).fetchall()
        by_id = {str(row["memory_id"]): row for row in rows}
        from .models import MemoryHit

        hits = []
        for memory_id, score in selected:
            row = by_id[memory_id]
            hits.append(
                MemoryHit(
                    record=self._row_to_record(row),
                    score=score + float(row["importance"]) * 0.01,
                    lexical_rank=lexical_ranks.get(memory_id),
                    dense_rank=dense_ranks.get(memory_id),
                    stale_revision=(
                        row["scope"] == "revision"
                        and row["repo_revision"] != context.revision
                    ),
                )
            )
        return MemorySearchResult(
            project_id=context.project_id,
            repo_revision=context.revision,
            query=request.query,
            hits=tuple(hits),
            embedding_model=self.embedding_client.model_id,
        )

    def forget(
        self,
        context: ProjectContext,
        memory_id: str,
        *,
        actor: str = "host",
        reason: str = "显式遗忘请求",
    ) -> None:
        with self._connection.transaction():
            with self._connection.cursor() as cursor:
                updated = cursor.execute(
                    """
                    UPDATE memories SET
                        content = '[已遗忘]', evidence_json = '[]', tags_json = '[]',
                        embedding = NULL, search_document = to_tsvector('simple', ''),
                        token_text = '', status = 'forgotten', updated_at = now()
                    WHERE project_id = %s AND memory_id = %s
                    """,
                    (context.project_id, memory_id),
                ).rowcount
                if updated == 0:
                    raise MemoryNotFoundError(f"找不到记忆：{memory_id}")
                cursor.execute(
                    """
                    INSERT INTO memory_lifecycle_events(
                        event_id, project_id, memory_id, event_type,
                        actor, reason, created_at
                    ) VALUES (%s, %s, %s, 'forgotten', %s, %s, now())
                    """,
                    (str(uuid4()), context.project_id, memory_id, actor, reason),
                )

    def expire(self, context: ProjectContext, *, now: datetime | None = None) -> MemoryMaintenanceReport:
        cutoff = now or _utc_now()
        with self._connection.transaction():
            with self._connection.cursor() as cursor:
                rows = cursor.execute(
                    """
                    SELECT memory_id FROM memories
                    WHERE project_id = %s AND status = 'active'
                      AND expires_at IS NOT NULL AND expires_at <= %s
                    FOR UPDATE
                    """,
                    (context.project_id, cutoff),
                ).fetchall()
                for row in rows:
                    memory_id = str(row["memory_id"])
                    cursor.execute(
                        """
                        UPDATE memories SET
                            content = '[已过期]', evidence_json = '[]',
                            tags_json = '[]', embedding = NULL,
                            search_document = to_tsvector('simple', ''),
                            token_text = '', status = 'expired', updated_at = now()
                        WHERE project_id = %s AND memory_id = %s
                        """,
                        (context.project_id, memory_id),
                    )
                    cursor.execute(
                        """
                        INSERT INTO memory_lifecycle_events(
                            event_id, project_id, memory_id, event_type,
                            actor, reason, created_at
                        ) VALUES (%s, %s, %s, 'expired', 'retention-policy', %s, now())
                        """,
                        (str(uuid4()), context.project_id, memory_id, "记忆达到 expires_at 保留期限"),
                    )
        return MemoryMaintenanceReport(
            project_id=context.project_id,
            expired_count=len(rows),
            forgotten_count=0,
            reembedded_count=0,
        )

    def reembed_project(self, context: ProjectContext) -> MemoryMaintenanceReport:
        with self._connection.cursor() as cursor:
            rows = cursor.execute(
                """
                SELECT memory_id, content FROM memories
                WHERE project_id = %s AND status = 'active'
                ORDER BY memory_id
                """,
                (context.project_id,),
            ).fetchall()
        vectors = self.embedding_client.embed_texts(tuple(str(row["content"]) for row in rows))
        if len(vectors) != len(rows):
            raise MemoryEmbeddingMismatchError("重建记忆向量时数量不一致")
        with self._connection.transaction():
            with self._connection.cursor() as cursor:
                for row, vector in zip(rows, vectors, strict=True):
                    cursor.execute(
                        """
                        UPDATE memories SET embedding = %s::vector,
                            embedding_model = %s, embedding_dimensions = %s,
                            updated_at = now()
                        WHERE project_id = %s AND memory_id = %s
                        """,
                        (
                            _vector_literal(tuple(vector)),
                            self.embedding_client.model_id,
                            self.embedding_client.dimensions,
                            context.project_id,
                            row["memory_id"],
                        ),
                    )
                cursor.execute(
                    """
                    INSERT INTO memory_embedding_state (
                        project_id, embedding_model, embedding_dimensions, updated_at
                    ) VALUES (%s, %s, %s, now())
                    ON CONFLICT(project_id) DO UPDATE SET
                        embedding_model = excluded.embedding_model,
                        embedding_dimensions = excluded.embedding_dimensions,
                        updated_at = excluded.updated_at
                    """,
                    (
                        context.project_id,
                        self.embedding_client.model_id,
                        self.embedding_client.dimensions,
                    ),
                )
        return MemoryMaintenanceReport(
            project_id=context.project_id,
            expired_count=0,
            forgotten_count=0,
            reembedded_count=len(rows),
        )

    def find_active_by_key(self, context: ProjectContext, memory_key: str):
        with self._connection.cursor() as cursor:
            row = cursor.execute(
                """
                SELECT * FROM memories
                WHERE project_id = %s AND memory_key = %s AND status = 'active'
                  AND (expires_at IS NULL OR expires_at > now())
                """,
                (context.project_id, memory_key),
            ).fetchone()
        return self._row_to_record(row) if row is not None else None

    def get_curation_decision(self, context: ProjectContext, candidate_id: str):
        with self._connection.cursor() as cursor:
            row = cursor.execute(
                """
                SELECT * FROM memory_curation_decisions
                WHERE project_id = %s AND candidate_id = %s
                """,
                (context.project_id, candidate_id),
            ).fetchone()
        if row is None:
            return None
        return self._curation_from_row(row)

    @staticmethod
    def _curation_from_row(row):
        from .models import MemoryCandidate, MemoryCurationDecision

        return MemoryCurationDecision(
            project_id=str(row["project_id"]),
            candidate=MemoryCandidate.model_validate_json(row["candidate_json"]),
            action=str(row["action"]),
            reason=str(row["reason"]),
            matched_memory_id=row["matched_memory_id"],
            result_memory_id=row["result_memory_id"],
            decided_by=row["decided_by"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def save_curation_decision(self, decision, *, replace: bool = False):
        command = (
            """
            INSERT INTO memory_curation_decisions (
                project_id, candidate_id, candidate_json, action, reason,
                matched_memory_id, result_memory_id, decided_by,
                created_at, updated_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT(project_id, candidate_id) DO UPDATE SET
                candidate_json = excluded.candidate_json,
                action = excluded.action,
                reason = excluded.reason,
                matched_memory_id = excluded.matched_memory_id,
                result_memory_id = excluded.result_memory_id,
                decided_by = excluded.decided_by,
                updated_at = excluded.updated_at
            """
            if replace
            else """
            INSERT INTO memory_curation_decisions (
                project_id, candidate_id, candidate_json, action, reason,
                matched_memory_id, result_memory_id, decided_by,
                created_at, updated_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT(project_id, candidate_id) DO NOTHING
            """
        )
        with self._connection.cursor() as cursor:
            cursor.execute(
                command,
                (
                    decision.project_id,
                    decision.candidate.candidate_id,
                    decision.candidate.model_dump_json(),
                    decision.action,
                    decision.reason,
                    decision.matched_memory_id,
                    decision.result_memory_id,
                    decision.decided_by,
                    decision.created_at,
                    decision.updated_at,
                ),
            )
            row = cursor.execute(
                """
                SELECT * FROM memory_curation_decisions
                WHERE project_id = %s AND candidate_id = %s
                """,
                (decision.project_id, decision.candidate.candidate_id),
            ).fetchone()
            self._connection.commit()
        return self._curation_from_row(row)

    def pending_reviews(self, context: ProjectContext, *, limit: int = 100):
        return self.list_pending_reviews(context, limit=limit)

    def list_pending_reviews(self, context: ProjectContext, *, limit: int = 100):
        with self._connection.cursor() as cursor:
            rows = cursor.execute(
                """
                SELECT * FROM memory_curation_decisions
                WHERE project_id = %s AND action = 'pending_review'
                ORDER BY created_at ASC LIMIT %s
                """,
                (context.project_id, limit),
            ).fetchall()
        return tuple(self._curation_from_row(row) for row in rows)

    def get_consolidation_run(self, consolidation_key: str) -> tuple[str, ...] | None:
        with self._connection.cursor() as cursor:
            row = cursor.execute(
                """
                SELECT result_candidate_ids_json FROM memory_consolidation_runs
                WHERE consolidation_key = %s
                """,
                (consolidation_key,),
            ).fetchone()
        if row is None:
            return None
        return tuple(str(item) for item in json.loads(row["result_candidate_ids_json"]))

    def save_consolidation_run(
        self,
        *,
        consolidation_key: str,
        project_id: str,
        topic: str,
        input_memory_ids: tuple[str, ...],
        model_id: str,
        prompt_version: str,
        result_candidate_ids: tuple[str, ...],
    ) -> None:
        with self._connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO memory_consolidation_runs (
                    consolidation_key, project_id, topic, input_memory_ids_json,
                    model_id, prompt_version, result_candidate_ids_json, created_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, now())
                ON CONFLICT(consolidation_key) DO NOTHING
                """,
                (
                    consolidation_key,
                    project_id,
                    topic,
                    json.dumps(input_memory_ids, ensure_ascii=False),
                    model_id,
                    prompt_version,
                    json.dumps(result_candidate_ids, ensure_ascii=False),
                ),
            )
            self._connection.commit()
