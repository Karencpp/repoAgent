"""项目隔离的 SQLite 长期记忆 Store。"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re
import sqlite3
from typing import Sequence
from uuid import uuid4

from repo_agent.projects import ProjectContext
from repo_agent.rag.embeddings import EmbeddingClient

from .models import (
    MemoryCandidate,
    MemoryCurationDecision,
    MemoryHit,
    MemoryLifecycleEvent,
    MemoryMaintenanceReport,
    MemoryRecord,
    MemorySearchRequest,
    MemorySearchResult,
    MemoryWrite,
)


_SEARCH_TOKEN_PATTERN = re.compile(
    r"[A-Za-z_][A-Za-z0-9_]*|\d+|[\u3400-\u4dbf\u4e00-\u9fff]+"
)


class MemoryStoreError(RuntimeError):
    """长期记忆存储错误的基类。"""


class MemoryNotFoundError(MemoryStoreError):
    """找不到指定项目内的记忆。"""


class MemoryEmbeddingMismatchError(MemoryStoreError):
    """当前向量客户端与记忆索引空间不一致。"""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _to_iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    normalized = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    return normalized.astimezone(timezone.utc).isoformat()


def _from_iso(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _fts_query(query: str) -> str | None:
    """将自由文本转成不含用户运算符的 FTS 查询。"""

    terms: list[str] = []
    for match in _SEARCH_TOKEN_PATTERN.finditer(query):
        token = match.group(0).casefold()
        if token not in terms:
            terms.append(token)
        if any("\u3400" <= char <= "\u9fff" for char in token) and len(token) > 1:
            for index in range(len(token) - 1):
                pair = token[index : index + 2]
                if pair not in terms:
                    terms.append(pair)
    if not terms:
        return None
    return " OR ".join(f'"{term}"' for term in terms[:32])


def _cosine(left: Sequence[float], right: Sequence[float]) -> float:
    """计算等维向量的余弦相似度。"""

    if len(left) != len(right):
        raise MemoryEmbeddingMismatchError("记忆查询向量维度不一致")
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if not left_norm or not right_norm:
        return 0.0
    return dot / (left_norm * right_norm)


class SQLiteMemoryStore:
    """保存跨线程记忆，并提供过滤后的混合检索。"""

    def __init__(
        self,
        storage_path: str | Path,
        embedding_client: EmbeddingClient,
    ) -> None:
        self.storage_path = Path(storage_path).expanduser().resolve()
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)
        self.embedding_client = embedding_client
        self._connection = sqlite3.connect(self.storage_path)
        self._connection.row_factory = sqlite3.Row
        self._initialize_schema()

    def close(self) -> None:
        """关闭数据库连接。"""

        self._connection.close()

    def __enter__(self) -> "SQLiteMemoryStore":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _initialize_schema(self) -> None:
        """创建记忆原文、向量、状态和 FTS5 表。"""

        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS memory_index_state (
                project_id TEXT PRIMARY KEY,
                embedding_model TEXT NOT NULL,
                embedding_dimensions INTEGER NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS memories (
                memory_id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                memory_key TEXT,
                memory_type TEXT NOT NULL,
                content TEXT NOT NULL,
                claim_status TEXT NOT NULL,
                importance REAL NOT NULL,
                scope TEXT NOT NULL,
                repo_revision TEXT,
                source TEXT NOT NULL,
                source_id TEXT NOT NULL,
                evidence_json TEXT NOT NULL,
                tags_json TEXT NOT NULL,
                status TEXT NOT NULL,
                supersedes_memory_id TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                expires_at TEXT,
                embedding_json TEXT NOT NULL,
                embedding_model TEXT NOT NULL,
                embedding_dimensions INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_memories_project_status
                ON memories(project_id, status);
            CREATE INDEX IF NOT EXISTS idx_memories_project_type
                ON memories(project_id, memory_type, claim_status);
            CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
                memory_id UNINDEXED,
                project_id UNINDEXED,
                content,
                tags
            );
            """
        )
        columns = {
            str(row["name"])
            for row in self._connection.execute(
                "PRAGMA table_info(memories)"
            ).fetchall()
        }
        if "memory_key" not in columns:
            self._connection.execute(
                "ALTER TABLE memories ADD COLUMN memory_key TEXT"
            )
        self._connection.executescript(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_memories_active_key
                ON memories(project_id, memory_key)
                WHERE status = 'active' AND memory_key IS NOT NULL;
            CREATE TABLE IF NOT EXISTS memory_curation_decisions (
                project_id TEXT NOT NULL,
                candidate_id TEXT NOT NULL,
                candidate_json TEXT NOT NULL,
                action TEXT NOT NULL,
                reason TEXT NOT NULL,
                matched_memory_id TEXT,
                result_memory_id TEXT,
                decided_by TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (project_id, candidate_id)
            );
            CREATE INDEX IF NOT EXISTS idx_memory_curation_pending
                ON memory_curation_decisions(project_id, action, created_at);
            CREATE TABLE IF NOT EXISTS memory_lifecycle_events (
                event_id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                memory_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                actor TEXT NOT NULL,
                reason TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_memory_lifecycle_project
                ON memory_lifecycle_events(project_id, created_at);
            CREATE TABLE IF NOT EXISTS memory_consolidation_runs (
                consolidation_key TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                topic TEXT NOT NULL,
                input_memory_ids_json TEXT NOT NULL,
                model_id TEXT NOT NULL,
                prompt_version TEXT NOT NULL,
                result_candidate_ids_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_memory_consolidation_project
                ON memory_consolidation_runs(project_id, topic, created_at);
            """
        )
        self._connection.commit()

    def _validate_embedding_state(self, project_id: str, *, allow_empty: bool) -> None:
        """确保同一项目没有混用不同向量空间。"""

        row = self._connection.execute(
            """
            SELECT embedding_model, embedding_dimensions
            FROM memory_index_state WHERE project_id = ?
            """,
            (project_id,),
        ).fetchone()
        if row is None:
            if allow_empty:
                return
            raise MemoryStoreError(f"项目尚无长期记忆：{project_id}")
        if (
            row["embedding_model"] != self.embedding_client.model_id
            or row["embedding_dimensions"] != self.embedding_client.dimensions
        ):
            raise MemoryEmbeddingMismatchError(
                "长期记忆使用了不同的 Embedding 模型或维度，必须先重建"
            )

    def _row_to_record(self, row: sqlite3.Row) -> MemoryRecord:
        """把 SQLite 行还原成严格记忆模型。"""

        return MemoryRecord(
            memory_id=str(row["memory_id"]),
            project_id=str(row["project_id"]),
            memory_key=(str(row["memory_key"]) if row["memory_key"] else None),
            memory_type=str(row["memory_type"]),
            content=str(row["content"]),
            claim_status=str(row["claim_status"]),
            importance=float(row["importance"]),
            scope=str(row["scope"]),
            repo_revision=(str(row["repo_revision"]) if row["repo_revision"] else None),
            source=str(row["source"]),
            source_id=str(row["source_id"]),
            evidence=tuple(json.loads(row["evidence_json"])),
            tags=tuple(json.loads(row["tags_json"])),
            status=str(row["status"]),
            supersedes_memory_id=(
                str(row["supersedes_memory_id"])
                if row["supersedes_memory_id"]
                else None
            ),
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
            expires_at=_from_iso(row["expires_at"]),
            embedding_model=str(row["embedding_model"]),
            embedding_dimensions=int(row["embedding_dimensions"]),
        )

    def _insert(
        self,
        context: ProjectContext,
        memory: MemoryWrite,
        embedding: tuple[float, ...],
        *,
        memory_key: str | None = None,
        supersedes_memory_id: str | None = None,
    ) -> MemoryRecord:
        """在当前事务中插入一条已经校验的记忆。"""

        memory_id = f"memory-{uuid4().hex}"
        now = _utc_now()
        values = (
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
            "active",
            supersedes_memory_id,
            _to_iso(now),
            _to_iso(now),
            _to_iso(memory.expires_at),
            json.dumps(embedding, separators=(",", ":")),
            self.embedding_client.model_id,
            self.embedding_client.dimensions,
        )
        self._connection.execute(
            """
            INSERT INTO memories (
                memory_id, project_id, memory_key, memory_type, content, claim_status,
                importance, scope, repo_revision, source, source_id,
                evidence_json, tags_json, status, supersedes_memory_id,
                created_at, updated_at, expires_at, embedding_json,
                embedding_model, embedding_dimensions
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            values,
        )
        self._connection.execute(
            """
            INSERT INTO memories_fts (memory_id, project_id, content, tags)
            VALUES (?, ?, ?, ?)
            """,
            (memory_id, context.project_id, memory.content, " ".join(memory.tags)),
        )
        self._connection.execute(
            """
            INSERT INTO memory_index_state (
                project_id, embedding_model, embedding_dimensions, updated_at
            ) VALUES (?, ?, ?, ?)
            ON CONFLICT(project_id) DO UPDATE SET
                embedding_model = excluded.embedding_model,
                embedding_dimensions = excluded.embedding_dimensions,
                updated_at = excluded.updated_at
            """,
            (
                context.project_id,
                self.embedding_client.model_id,
                self.embedding_client.dimensions,
                _to_iso(now),
            ),
        )
        row = self._connection.execute(
            "SELECT * FROM memories WHERE memory_id = ?",
            (memory_id,),
        ).fetchone()
        return self._row_to_record(row)

    def _embed_memory(self, memory: MemoryWrite) -> tuple[float, ...]:
        """在数据库事务外生成并校验单条记忆向量。"""

        vectors = self.embedding_client.embed_texts((memory.content,))
        if len(vectors) != 1 or len(vectors[0]) != self.embedding_client.dimensions:
            raise MemoryEmbeddingMismatchError("记忆 Embedding 数量或维度错误")
        return tuple(vectors[0])

    def put(self, context: ProjectContext, memory: MemoryWrite) -> MemoryRecord:
        """保存一条长期记忆。"""

        return self.put_with_key(context, memory, memory_key=None)

    def put_with_key(
        self,
        context: ProjectContext,
        memory: MemoryWrite,
        *,
        memory_key: str | None,
    ) -> MemoryRecord:
        """保存一条可由 Curator 稳定寻址的长期记忆。"""

        self._validate_embedding_state(context.project_id, allow_empty=True)
        embedding = self._embed_memory(memory)
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            record = self._insert(
                context,
                memory,
                embedding,
                memory_key=memory_key,
            )
            self._connection.commit()
            return record
        except Exception:
            self._connection.rollback()
            raise

    def supersede(
        self,
        context: ProjectContext,
        old_memory_id: str,
        replacement: MemoryWrite,
    ) -> MemoryRecord:
        """原子写入新事实并把旧事实标记为已替代。"""

        self._validate_embedding_state(context.project_id, allow_empty=False)
        old = self._connection.execute(
            """
            SELECT * FROM memories
            WHERE memory_id = ? AND project_id = ? AND status = 'active'
            """,
            (old_memory_id, context.project_id),
        ).fetchone()
        if old is None:
            raise MemoryNotFoundError(f"找不到可替代的记忆：{old_memory_id}")
        embedding = self._embed_memory(replacement)
        replacement_key = (
            str(old["memory_key"]) if old["memory_key"] else None
        )
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            self._connection.execute(
                """
                UPDATE memories SET status = 'superseded', updated_at = ?
                WHERE memory_id = ? AND project_id = ?
                """,
                (_to_iso(_utc_now()), old_memory_id, context.project_id),
            )
            record = self._insert(
                context,
                replacement,
                embedding,
                memory_key=replacement_key,
                supersedes_memory_id=old_memory_id,
            )
            self._connection.commit()
            return record
        except Exception:
            self._connection.rollback()
            raise

    def _insert_lifecycle_event(
        self,
        context: ProjectContext,
        memory_id: str,
        *,
        event_type: str,
        actor: str,
        reason: str,
        created_at: datetime,
    ) -> None:
        """在当前事务内写入不可变生命周期审计事件。"""

        self._connection.execute(
            """
            INSERT INTO memory_lifecycle_events(
                event_id, project_id, memory_id, event_type,
                actor, reason, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(uuid4()),
                context.project_id,
                memory_id,
                event_type,
                actor,
                reason,
                _to_iso(created_at),
            ),
        )

    def forget(
        self,
        context: ProjectContext,
        memory_id: str,
        *,
        actor: str = "host",
        reason: str = "显式遗忘请求",
    ) -> None:
        """擦除正文、向量和 FTS，只保留最小遗忘墓碑。"""

        if not actor.strip() or not reason.strip():
            raise ValueError("遗忘执行者和原因不能为空")

        row = self._connection.execute(
            "SELECT status FROM memories WHERE memory_id = ? AND project_id = ?",
            (memory_id, context.project_id),
        ).fetchone()
        if row is None:
            raise MemoryNotFoundError(f"找不到记忆：{memory_id}")
        now = _utc_now()
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            self._connection.execute(
                "DELETE FROM memories_fts WHERE memory_id = ?",
                (memory_id,),
            )
            self._connection.execute(
                """
                UPDATE memories SET
                    content = '[已遗忘]', evidence_json = '[]', tags_json = '[]',
                    embedding_json = '[]', status = 'forgotten', updated_at = ?
                WHERE memory_id = ? AND project_id = ?
                """,
                (_to_iso(now), memory_id, context.project_id),
            )
            self._insert_lifecycle_event(
                context,
                memory_id,
                event_type="forgotten",
                actor=actor,
                reason=reason,
                created_at=now,
            )
            self._connection.commit()
        except Exception:
            self._connection.rollback()
            raise

    def expire(self, context: ProjectContext, *, now: datetime | None = None) -> MemoryMaintenanceReport:
        """擦除达到 TTL 的活动记忆。"""

        cutoff = _to_iso(now or _utc_now())
        rows = self._connection.execute(
            """
            SELECT memory_id FROM memories
            WHERE project_id = ? AND status = 'active'
              AND expires_at IS NOT NULL AND expires_at <= ?
            """,
            (context.project_id, cutoff),
        ).fetchall()
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            for row in rows:
                self._connection.execute(
                    "DELETE FROM memories_fts WHERE memory_id = ?",
                    (row["memory_id"],),
                )
                self._connection.execute(
                    """
                    UPDATE memories SET
                        content = '[已过期]', evidence_json = '[]', tags_json = '[]',
                        embedding_json = '[]', status = 'expired', updated_at = ?
                    WHERE memory_id = ? AND project_id = ?
                    """,
                    (cutoff, row["memory_id"], context.project_id),
                )
                self._insert_lifecycle_event(
                    context,
                    str(row["memory_id"]),
                    event_type="expired",
                    actor="retention-policy",
                    reason="记忆达到 expires_at 保留期限",
                    created_at=now or _utc_now(),
                )
            self._connection.commit()
        except Exception:
            self._connection.rollback()
            raise
        return MemoryMaintenanceReport(
            project_id=context.project_id,
            expired_count=len(rows),
            forgotten_count=0,
            reembedded_count=0,
        )

    def list_lifecycle_events(
        self,
        context: ProjectContext,
        *,
        memory_id: str | None = None,
        limit: int = 100,
    ) -> tuple[MemoryLifecycleEvent, ...]:
        """读取当前项目的遗忘与过期审计轨迹。"""

        if not 1 <= limit <= 1_000:
            raise ValueError("limit 必须在 1 到 1000 之间")
        clauses = ["project_id = ?"]
        params: list[object] = [context.project_id]
        if memory_id is not None:
            clauses.append("memory_id = ?")
            params.append(memory_id)
        params.append(limit)
        rows = self._connection.execute(
            f"""
            SELECT * FROM memory_lifecycle_events
            WHERE {' AND '.join(clauses)}
            ORDER BY created_at DESC, event_id DESC
            LIMIT ?
            """,
            tuple(params),
        ).fetchall()
        return tuple(
            MemoryLifecycleEvent(
                event_id=str(row["event_id"]),
                project_id=str(row["project_id"]),
                memory_id=str(row["memory_id"]),
                event_type=str(row["event_type"]),
                actor=str(row["actor"]),
                reason=str(row["reason"]),
                created_at=datetime.fromisoformat(str(row["created_at"])),
            )
            for row in rows
        )

    def _filtered_rows(
        self,
        context: ProjectContext,
        request: MemorySearchRequest,
    ) -> list[sqlite3.Row]:
        """先使用确定性元数据条件缩小候选范围。"""

        type_placeholders = ",".join("?" for _ in request.memory_types)
        claim_placeholders = ",".join("?" for _ in request.claim_statuses)
        revision_clause = (
            ""
            if request.include_stale_revisions
            else "AND (scope = 'project' OR repo_revision = ?)"
        )
        parameters: list[object] = [
            context.project_id,
            *request.memory_types,
            *request.claim_statuses,
            request.min_importance,
            _to_iso(_utc_now()),
        ]
        if not request.include_stale_revisions:
            parameters.append(context.revision)
        return self._connection.execute(
            f"""
            SELECT * FROM memories
            WHERE project_id = ? AND status = 'active'
              AND memory_type IN ({type_placeholders})
              AND claim_status IN ({claim_placeholders})
              AND importance >= ?
              AND (expires_at IS NULL OR expires_at > ?)
              {revision_clause}
            ORDER BY memory_id
            """,
            tuple(parameters),
        ).fetchall()

    def search(
        self,
        context: ProjectContext,
        request: MemorySearchRequest,
    ) -> MemorySearchResult:
        """在元数据过滤后融合 BM25、向量排名和重要性。"""

        rows = self._filtered_rows(context, request)
        if not rows:
            return MemorySearchResult(
                project_id=context.project_id,
                repo_revision=context.revision,
                query=request.query,
                hits=(),
                embedding_model=self.embedding_client.model_id,
            )
        self._validate_embedding_state(context.project_id, allow_empty=False)
        allowed_ids = {str(row["memory_id"]) for row in rows}
        candidate_limit = min(80, request.top_k * 4)
        match_query = _fts_query(request.query)
        lexical_ids: list[str] = []
        if match_query is not None:
            lexical_rows = self._connection.execute(
                """
                SELECT memory_id, bm25(memories_fts, 0.0, 0.0, 1.0, 1.5) AS rank_score
                FROM memories_fts
                WHERE memories_fts MATCH ? AND project_id = ?
                ORDER BY rank_score ASC
                LIMIT ?
                """,
                (match_query, context.project_id, candidate_limit * 4),
            ).fetchall()
            lexical_ids = [
                str(row["memory_id"])
                for row in lexical_rows
                if str(row["memory_id"]) in allowed_ids
            ][:candidate_limit]

        query_vectors = self.embedding_client.embed_texts((request.query,))
        if len(query_vectors) != 1:
            raise MemoryEmbeddingMismatchError("记忆查询必须返回一个向量")
        query_vector = query_vectors[0]
        dense_scored: list[tuple[str, float]] = []
        if any(query_vector):
            for row in rows:
                vector = tuple(float(value) for value in json.loads(row["embedding_json"]))
                score = _cosine(query_vector, vector)
                if score > 0:
                    dense_scored.append((str(row["memory_id"]), score))
        dense_scored.sort(key=lambda item: (-item[1], item[0]))
        dense_ids = [memory_id for memory_id, _ in dense_scored[:candidate_limit]]

        lexical_ranks = {memory_id: rank for rank, memory_id in enumerate(lexical_ids, 1)}
        dense_ranks = {memory_id: rank for rank, memory_id in enumerate(dense_ids, 1)}
        by_id = {str(row["memory_id"]): row for row in rows}
        fused: list[tuple[str, float]] = []
        for memory_id in set(lexical_ranks) | set(dense_ranks):
            row = by_id[memory_id]
            score = float(row["importance"]) * 0.01
            if memory_id in lexical_ranks:
                score += 1.0 / (60 + lexical_ranks[memory_id])
            if memory_id in dense_ranks:
                score += 1.0 / (60 + dense_ranks[memory_id])
            fused.append((memory_id, score))
        fused.sort(key=lambda item: (-item[1], item[0]))

        hits = tuple(
            MemoryHit(
                record=self._row_to_record(by_id[memory_id]),
                score=score,
                lexical_rank=lexical_ranks.get(memory_id),
                dense_rank=dense_ranks.get(memory_id),
                stale_revision=(
                    by_id[memory_id]["scope"] == "revision"
                    and by_id[memory_id]["repo_revision"] != context.revision
                ),
            )
            for memory_id, score in fused[: request.top_k]
        )
        return MemorySearchResult(
            project_id=context.project_id,
            repo_revision=context.revision,
            query=request.query,
            hits=hits,
            embedding_model=self.embedding_client.model_id,
        )

    def get(self, context: ProjectContext, memory_id: str) -> MemoryRecord:
        """按项目读取一条记忆，包括非活动墓碑。"""

        row = self._connection.execute(
            "SELECT * FROM memories WHERE project_id = ? AND memory_id = ?",
            (context.project_id, memory_id),
        ).fetchone()
        if row is None:
            raise MemoryNotFoundError(f"找不到记忆：{memory_id}")
        return self._row_to_record(row)

    def find_active_by_key(
        self,
        context: ProjectContext,
        memory_key: str,
    ) -> MemoryRecord | None:
        """按 Curator 稳定事实键读取当前活动版本。"""

        row = self._connection.execute(
            """
            SELECT * FROM memories
            WHERE project_id = ? AND memory_key = ? AND status = 'active'
              AND (expires_at IS NULL OR expires_at > ?)
            """,
            (context.project_id, memory_key, _to_iso(_utc_now())),
        ).fetchone()
        return self._row_to_record(row) if row is not None else None

    @staticmethod
    def _curation_from_row(row: sqlite3.Row) -> MemoryCurationDecision:
        """把候选审计行恢复成严格领域模型。"""

        return MemoryCurationDecision(
            project_id=str(row["project_id"]),
            candidate=MemoryCandidate.model_validate_json(row["candidate_json"]),
            action=str(row["action"]),
            reason=str(row["reason"]),
            matched_memory_id=(
                str(row["matched_memory_id"])
                if row["matched_memory_id"]
                else None
            ),
            result_memory_id=(
                str(row["result_memory_id"])
                if row["result_memory_id"]
                else None
            ),
            decided_by=(str(row["decided_by"]) if row["decided_by"] else None),
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    def get_curation_decision(
        self,
        context: ProjectContext,
        candidate_id: str,
    ) -> MemoryCurationDecision | None:
        """读取候选的幂等决策记录。"""

        row = self._connection.execute(
            """
            SELECT * FROM memory_curation_decisions
            WHERE project_id = ? AND candidate_id = ?
            """,
            (context.project_id, candidate_id),
        ).fetchone()
        return self._curation_from_row(row) if row is not None else None

    def save_curation_decision(
        self,
        decision: MemoryCurationDecision,
        *,
        replace: bool = False,
    ) -> MemoryCurationDecision:
        """持久化候选决策，候选 id 默认保持幂等。"""

        values = (
            decision.project_id,
            decision.candidate.candidate_id,
            decision.candidate.model_dump_json(),
            decision.action,
            decision.reason,
            decision.matched_memory_id,
            decision.result_memory_id,
            decision.decided_by,
            _to_iso(decision.created_at),
            _to_iso(decision.updated_at),
        )
        command = (
            """
            INSERT INTO memory_curation_decisions (
                project_id, candidate_id, candidate_json, action, reason,
                matched_memory_id, result_memory_id, decided_by,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            INSERT OR IGNORE INTO memory_curation_decisions (
                project_id, candidate_id, candidate_json, action, reason,
                matched_memory_id, result_memory_id, decided_by,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """
        )
        self._connection.execute(command, values)
        self._connection.commit()
        row = self._connection.execute(
            """
            SELECT * FROM memory_curation_decisions
            WHERE project_id = ? AND candidate_id = ?
            """,
            (decision.project_id, decision.candidate.candidate_id),
        ).fetchone()
        if row is None:
            raise MemoryStoreError("候选决策持久化失败")
        return self._curation_from_row(row)

    def get_consolidation_run(
        self,
        consolidation_key: str,
    ) -> tuple[str, ...] | None:
        """按稳定归纳键读取已提交过的候选 ID。"""

        row = self._connection.execute(
            """
            SELECT result_candidate_ids_json FROM memory_consolidation_runs
            WHERE consolidation_key = ?
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
        """记录一次语义归纳慢路径的输入、模型版本和输出候选。"""

        self._connection.execute(
            """
            INSERT OR IGNORE INTO memory_consolidation_runs (
                consolidation_key, project_id, topic, input_memory_ids_json,
                model_id, prompt_version, result_candidate_ids_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                consolidation_key,
                project_id,
                topic,
                json.dumps(input_memory_ids, ensure_ascii=False),
                model_id,
                prompt_version,
                json.dumps(result_candidate_ids, ensure_ascii=False),
                _to_iso(_utc_now()),
            ),
        )
        self._connection.commit()

    def list_pending_reviews(
        self,
        context: ProjectContext,
        *,
        limit: int = 100,
    ) -> tuple[MemoryCurationDecision, ...]:
        """列出当前项目等待人工审核的候选。"""

        if limit < 1 or limit > 500:
            raise ValueError("待审核列表 limit 必须在 1 到 500 之间")
        rows = self._connection.execute(
            """
            SELECT * FROM memory_curation_decisions
            WHERE project_id = ? AND action = 'pending_review'
            ORDER BY created_at ASC LIMIT ?
            """,
            (context.project_id, limit),
        ).fetchall()
        return tuple(self._curation_from_row(row) for row in rows)

    def reembed_project(self, context: ProjectContext) -> MemoryMaintenanceReport:
        """在更换 Embedding 模型后重建项目全部活动记忆向量。"""

        rows = self._connection.execute(
            """
            SELECT memory_id, content FROM memories
            WHERE project_id = ? AND status = 'active'
            ORDER BY memory_id
            """,
            (context.project_id,),
        ).fetchall()
        vectors = self.embedding_client.embed_texts(
            tuple(str(row["content"]) for row in rows)
        )
        if len(vectors) != len(rows) or any(
            len(vector) != self.embedding_client.dimensions for vector in vectors
        ):
            raise MemoryEmbeddingMismatchError("重建记忆向量时数量或维度错误")
        now = _to_iso(_utc_now())
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            for row, vector in zip(rows, vectors, strict=True):
                self._connection.execute(
                    """
                    UPDATE memories SET embedding_json = ?, embedding_model = ?,
                        embedding_dimensions = ?, updated_at = ?
                    WHERE memory_id = ? AND project_id = ?
                    """,
                    (
                        json.dumps(vector, separators=(",", ":")),
                        self.embedding_client.model_id,
                        self.embedding_client.dimensions,
                        now,
                        row["memory_id"],
                        context.project_id,
                    ),
                )
            self._connection.execute(
                """
                INSERT INTO memory_index_state (
                    project_id, embedding_model, embedding_dimensions, updated_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(project_id) DO UPDATE SET
                    embedding_model = excluded.embedding_model,
                    embedding_dimensions = excluded.embedding_dimensions,
                    updated_at = excluded.updated_at
                """,
                (
                    context.project_id,
                    self.embedding_client.model_id,
                    self.embedding_client.dimensions,
                    now,
                ),
            )
            self._connection.commit()
        except Exception:
            self._connection.rollback()
            raise
        return MemoryMaintenanceReport(
            project_id=context.project_id,
            expired_count=0,
            forgotten_count=0,
            reembedded_count=len(rows),
        )

    def replace(
        self,
        context: ProjectContext,
        old_memory_id: str,
        replacement: MemoryWrite,
    ) -> MemoryRecord:
        """MemoryStorePort 兼容入口：替换活动记忆。"""

        return self.supersede(context, old_memory_id, replacement)
