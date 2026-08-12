"""PostgreSQL/pgvector 代码库索引后端。"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Literal

from repo_agent.postgres_vectors import hnsw_vector_type
from repo_agent.projects import ProjectContext, inspect_repository
from repo_agent.rag.chunking import RepositoryChunker
from repo_agent.rag.embeddings import EmbeddingClient
from repo_agent.rag.index import (
    HybridSearchConfig,
    RAGEmbeddingMismatchError,
    RAGRevisionMismatchError,
    build_fts_or_query,
)
from repo_agent.rag.models import IndexingReport, RepositoryChunkDraft, RetrievalHit, RetrievalResult


class PostgresRAGError(RuntimeError):
    """PostgreSQL RAG 后端错误。"""


@dataclass(frozen=True, slots=True)
class _PreparedChunk:
    """待批量写入的分块。"""

    chunk_id: str
    draft: RepositoryChunkDraft
    embedding: tuple[float, ...]


def _require_psycopg():
    """惰性导入 psycopg，避免 SQLite 默认模式引入可选依赖。"""

    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError as exc:
        raise PostgresRAGError("PostgreSQL 后端需要安装可选依赖：repo-agent[postgres]") from exc
    return psycopg, dict_row


def _chunk_id(project_id: str, draft: RepositoryChunkDraft) -> str:
    payload = f"{project_id}\0{draft.path}\0{draft.start_line}\0{draft.end_line}\0{draft.content_hash}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _vector_literal(values: tuple[float, ...]) -> str:
    """把向量序列转换为 pgvector 文本字面量。"""

    return "[" + ",".join(f"{value:.12g}" for value in values) + "]"


class PostgresRAGIndex:
    """使用 PostgreSQL FTS、trigram 和 pgvector HNSW 的 RAG 后端。"""

    def __init__(
        self,
        dsn: str,
        embedding_client: EmbeddingClient,
        *,
        chunker: RepositoryChunker | None = None,
        search_config: HybridSearchConfig | None = None,
    ) -> None:
        psycopg, dict_row = _require_psycopg()
        self.embedding_client = embedding_client
        self._vector_type = hnsw_vector_type(embedding_client.dimensions)
        self.chunker = chunker or RepositoryChunker()
        self.search_config = search_config or HybridSearchConfig()
        # Reads must not leave an implicit outer transaction open. Writes use
        # explicit transaction() blocks below so they remain atomic.
        self._connection = psycopg.connect(dsn, row_factory=dict_row, autocommit=True)

    def close(self) -> None:
        """关闭 PostgreSQL 连接。"""

        self._connection.close()

    def _validate_state(self, context: ProjectContext) -> None:
        with self._connection.cursor() as cursor:
            row = cursor.execute(
                """
                SELECT repo_revision, embedding_model, embedding_dimensions
                FROM repository_index_state WHERE project_id = %s
                """,
                (context.project_id,),
            ).fetchone()
        if row is None:
            raise PostgresRAGError(f"项目尚未建立 RAG 索引：{context.project_id}")
        if row["repo_revision"] != context.revision:
            raise RAGRevisionMismatchError("RAG 索引版本与当前代码库版本不一致")
        if (
            row["embedding_model"] != self.embedding_client.model_id
            or row["embedding_dimensions"] != self.embedding_client.dimensions
        ):
            raise RAGEmbeddingMismatchError("RAG 索引向量空间与当前客户端不一致")

    def index_repository(self, context: ProjectContext) -> IndexingReport:
        """在项目 advisory lock 下执行事务化增量索引。"""

        if inspect_repository(context.repo_root).revision != context.revision:
            raise RAGRevisionMismatchError("传入的 ProjectContext 已过期")
        scan = self.chunker.scan(context)
        current_files = {item.path: item for item in scan.files}
        with self._connection.cursor() as cursor:
            rows = cursor.execute(
                "SELECT path, file_hash FROM repository_files WHERE project_id = %s",
                (context.project_id,),
            ).fetchall()
            state = cursor.execute(
                """
                SELECT embedding_model, embedding_dimensions
                FROM repository_index_state WHERE project_id = %s
                """,
                (context.project_id,),
            ).fetchone()
        existing_hashes = {str(row["path"]): str(row["file_hash"]) for row in rows}
        model_changed = state is not None and (
            state["embedding_model"] != self.embedding_client.model_id
            or state["embedding_dimensions"] != self.embedding_client.dimensions
        )
        deleted_paths = sorted(set(existing_hashes) - set(current_files))
        changed_paths = sorted(
            path
            for path, source in current_files.items()
            if model_changed or existing_hashes.get(path) != source.content_hash
        )
        unchanged_paths = sorted(set(current_files) - set(changed_paths))
        drafts_by_path: dict[str, tuple[RepositoryChunkDraft, ...]] = {}
        all_drafts: list[RepositoryChunkDraft] = []
        for path in changed_paths:
            drafts = self.chunker.chunk(current_files[path])
            drafts_by_path[path] = drafts
            all_drafts.extend(drafts)
        embeddings = self.embedding_client.embed_texts(tuple(draft.embedding_text() for draft in all_drafts))
        if len(embeddings) != len(all_drafts):
            raise RAGEmbeddingMismatchError("Embedding 返回数量与分块数量不一致")
        prepared = tuple(
            _PreparedChunk(_chunk_id(context.project_id, draft), draft, tuple(vector))
            for draft, vector in zip(all_drafts, embeddings, strict=True)
        )
        if inspect_repository(context.repo_root).revision != context.revision:
            raise RAGRevisionMismatchError("代码库在索引过程中发生变化，本次结果未写入")
        affected_paths = sorted(set(deleted_paths) | set(changed_paths))
        try:
            with self._connection.transaction():
                with self._connection.cursor() as cursor:
                    cursor.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (context.project_id,))
                    for path in affected_paths:
                        cursor.execute(
                            "DELETE FROM repository_chunks WHERE project_id = %s AND path = %s",
                            (context.project_id, path),
                        )
                        cursor.execute(
                            "DELETE FROM repository_files WHERE project_id = %s AND path = %s",
                            (context.project_id, path),
                        )
                    for item in prepared:
                        draft = item.draft
                        cursor.execute(
                            """
                            INSERT INTO repository_chunks (
                                chunk_id, project_id, repo_revision, path, start_line,
                                end_line, kind, language, symbol, content, content_hash,
                                embedding_model, embedding_dimensions, embedding,
                                search_document, token_text, heading_path_json
                            ) VALUES (
                                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                                %s, %s, %s::vector,
                                setweight(to_tsvector('simple', %s), 'C') ||
                                setweight(to_tsvector('simple', %s), 'B') ||
                                setweight(to_tsvector('simple', %s), 'A'),
                                %s, %s
                            )
                            """,
                            (
                                item.chunk_id,
                                context.project_id,
                                context.revision,
                                draft.path,
                                draft.start_line,
                                draft.end_line,
                                draft.kind,
                                draft.language,
                                draft.symbol,
                                draft.content,
                                draft.content_hash,
                                self.embedding_client.model_id,
                                self.embedding_client.dimensions,
                                _vector_literal(item.embedding),
                                draft.path,
                                draft.content,
                                f"{draft.symbol or ''} {' '.join(draft.heading_path)}",
                                f"{draft.path} {draft.symbol or ''} {' '.join(draft.heading_path)}",
                                json.dumps(draft.heading_path, ensure_ascii=False),
                            ),
                        )
                    for path in changed_paths:
                        source = current_files[path]
                        cursor.execute(
                            """
                            INSERT INTO repository_files (
                                project_id, repo_revision, path, file_hash, chunk_count
                            ) VALUES (%s, %s, %s, %s, %s)
                            """,
                            (
                                context.project_id,
                                context.revision,
                                path,
                                source.content_hash,
                                len(drafts_by_path[path]),
                            ),
                        )
                    cursor.execute(
                        """
                        INSERT INTO repository_index_state (
                            project_id, repo_revision, embedding_model,
                            embedding_dimensions, updated_at
                        ) VALUES (%s, %s, %s, %s, now())
                        ON CONFLICT(project_id) DO UPDATE SET
                            repo_revision = excluded.repo_revision,
                            embedding_model = excluded.embedding_model,
                            embedding_dimensions = excluded.embedding_dimensions,
                            updated_at = excluded.updated_at
                        """,
                        (
                            context.project_id,
                            context.revision,
                            self.embedding_client.model_id,
                            self.embedding_client.dimensions,
                        ),
                    )
        except Exception:
            self._connection.rollback()
            raise
        return IndexingReport(
            project_id=context.project_id,
            repo_revision=context.revision,
            embedding_model=self.embedding_client.model_id,
            embedding_dimensions=self.embedding_client.dimensions,
            scanned_files=len(scan.files),
            indexed_files=len(changed_paths),
            unchanged_files=len(unchanged_paths),
            deleted_files=len(deleted_paths),
            skipped_files=scan.skipped_files,
            written_chunks=len(prepared),
        )

    def _rank_ids(self, context: ProjectContext, query: str, limit: int, mode: str) -> tuple[list[str], list[str]]:
        query_vector = (
            self.embedding_client.embed_texts((query,))[0]
            if mode in {"hybrid", "dense"}
            else ()
        )
        lexical_query = build_fts_or_query(query)
        with self._connection.cursor() as cursor:
            lexical_rows = []
            dense_rows = []
            if mode in {"hybrid", "lexical"} and lexical_query is not None:
                lexical_rows = cursor.execute(
                    """
                    SELECT chunk_id
                    FROM repository_chunks
                    WHERE project_id = %s AND repo_revision = %s
                      AND embedding_model = %s AND embedding_dimensions = %s
                      AND search_document @@ websearch_to_tsquery('simple', %s)
                    ORDER BY ts_rank_cd(search_document, websearch_to_tsquery('simple', %s)) DESC, chunk_id
                    LIMIT %s
                    """,
                    (
                        context.project_id,
                        context.revision,
                        self.embedding_client.model_id,
                        self.embedding_client.dimensions,
                        lexical_query,
                        lexical_query,
                        limit,
                    ),
                ).fetchall()
            if mode in {"hybrid", "dense"} and any(query_vector):
                dimensions = self.embedding_client.dimensions
                dense_rows = cursor.execute(
                    f"""
                    SELECT chunk_id
                    FROM repository_chunks
                    WHERE project_id = %s AND repo_revision = %s
                      AND embedding_model = %s AND embedding_dimensions = {dimensions}
                    ORDER BY (embedding::{self._vector_type}) <=> %s::{self._vector_type}
                    LIMIT %s
                    """,
                    (
                        context.project_id,
                        context.revision,
                        self.embedding_client.model_id,
                        _vector_literal(tuple(query_vector)),
                        limit,
                    ),
                ).fetchall()
        return [str(row["chunk_id"]) for row in lexical_rows], [str(row["chunk_id"]) for row in dense_rows]

    def search(
        self,
        context: ProjectContext,
        query: str,
        *,
        top_k: int = 5,
        mode: Literal["hybrid", "lexical", "dense"] = "hybrid",
    ) -> RetrievalResult:
        """在数据库内完成候选过滤、FTS 和 ANN，再用 RRF 融合。"""

        if not 1 <= top_k <= 20:
            raise ValueError("top_k 必须在 1 到 20 之间")
        normalized = query.strip()
        if not normalized:
            raise ValueError("query 不能为空")
        self._validate_state(context)
        pool_size = top_k * self.search_config.candidate_pool_multiplier
        lexical_ids, dense_ids = self._rank_ids(context, normalized, pool_size, mode)
        lexical_ranks = {chunk_id: rank for rank, chunk_id in enumerate(lexical_ids, 1)}
        dense_ranks = {chunk_id: rank for rank, chunk_id in enumerate(dense_ids, 1)}
        fused: list[tuple[str, float]] = []
        for chunk_id in set(lexical_ranks) | set(dense_ranks):
            score = 0.0
            if chunk_id in lexical_ranks:
                score += self.search_config.lexical_weight / (self.search_config.rrf_k + lexical_ranks[chunk_id])
            if chunk_id in dense_ranks:
                score += self.search_config.dense_weight / (self.search_config.rrf_k + dense_ranks[chunk_id])
            fused.append((chunk_id, score))
        fused.sort(key=lambda item: (-item[1], item[0]))
        selected = fused[:top_k]
        if not selected:
            return RetrievalResult(
                project_id=context.project_id,
                repo_revision=context.revision,
                query=normalized,
                hits=(),
                lexical_candidates=len(lexical_ids),
                dense_candidates=len(dense_ids),
                embedding_model=self.embedding_client.model_id,
            )
        with self._connection.cursor() as cursor:
            rows = cursor.execute(
                "SELECT * FROM repository_chunks WHERE chunk_id = ANY(%s)",
                ([chunk_id for chunk_id, _score in selected],),
            ).fetchall()
        by_id = {str(row["chunk_id"]): row for row in rows}
        hits = []
        for chunk_id, score in selected:
            row = by_id.get(chunk_id)
            if row is None:
                continue
            hits.append(
                RetrievalHit(
                    chunk_id=chunk_id,
                    path=str(row["path"]),
                    kind=str(row["kind"]),
                    language=str(row["language"]),
                    start_line=int(row["start_line"]),
                    end_line=int(row["end_line"]),
                    content=str(row["content"]),
                    content_hash=str(row["content_hash"]),
                    symbol=row["symbol"],
                    heading_path=tuple(json.loads(row["heading_path_json"] or "[]")),
                    citation=f"{row['path']}:{row['start_line']}-{row['end_line']}",
                    score=score,
                    lexical_rank=lexical_ranks.get(chunk_id),
                    dense_rank=dense_ranks.get(chunk_id),
                )
            )
        return RetrievalResult(
            project_id=context.project_id,
            repo_revision=context.revision,
            query=normalized,
            hits=tuple(hits),
            lexical_candidates=len(lexical_ids),
            dense_candidates=len(dense_ids),
            embedding_model=self.embedding_client.model_id,
        )

    def delete_project(self, project_id: str) -> None:
        """事务化删除一个项目的全部 RAG 索引。"""

        with self._connection.transaction():
            with self._connection.cursor() as cursor:
                cursor.execute("DELETE FROM repository_chunks WHERE project_id = %s", (project_id,))
                cursor.execute("DELETE FROM repository_files WHERE project_id = %s", (project_id,))
                cursor.execute("DELETE FROM repository_index_state WHERE project_id = %s", (project_id,))
