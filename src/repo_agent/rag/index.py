"""基于 SQLite FTS5 和向量余弦相似度的代码库混合索引。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3
from typing import Literal, Sequence

from repo_agent.projects import ProjectContext, inspect_repository

from .chunking import RepositoryChunker
from .embeddings import EmbeddingClient
from .models import (
    IndexingReport,
    RepositoryChunkDraft,
    RetrievalHit,
    RetrievalResult,
)


_SEARCH_TOKEN_PATTERN = re.compile(
    r"[A-Za-z_][A-Za-z0-9_]*|\d+|[\u3400-\u4dbf\u4e00-\u9fff]+"
)
_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_LEXICAL_QUERY_STOP_WORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "class",
        "code",
        "defined",
        "django",
        "do",
        "does",
        "find",
        "for",
        "function",
        "how",
        "implemented",
        "implementation",
        "in",
        "is",
        "method",
        "module",
        "of",
        "or",
        "the",
        "to",
        "what",
        "where",
        "which",
        "with",
    }
)


class RAGIndexError(RuntimeError):
    """索引建立和检索错误的基类。"""


class RAGIndexNotReadyError(RAGIndexError):
    """目标项目尚未建立可用索引。"""


class RAGRevisionMismatchError(RAGIndexError):
    """索引版本与当前目标仓库版本不一致。"""


class RAGEmbeddingMismatchError(RAGIndexError):
    """向量模型或维度与索引记录不一致。"""


@dataclass(frozen=True, slots=True)
class HybridSearchConfig:
    """混合召回的候选池和 RRF 参数。"""

    candidate_pool_multiplier: int = 4
    rrf_k: int = 60
    lexical_weight: float = 1.0
    dense_weight: float = 1.0
    max_dense_scan_chunks: int = 50_000

    def __post_init__(self) -> None:
        if not 1 <= self.candidate_pool_multiplier <= 20:
            raise ValueError("candidate_pool_multiplier 必须在 1 到 20 之间")
        if self.rrf_k < 1:
            raise ValueError("rrf_k 必须大于等于 1")
        if self.lexical_weight < 0 or self.dense_weight < 0:
            raise ValueError("检索权重不能为负数")
        if self.lexical_weight == 0 and self.dense_weight == 0:
            raise ValueError("至少一个检索权重必须大于 0")
        if self.max_dense_scan_chunks < 1:
            raise ValueError("max_dense_scan_chunks 必须大于等于 1")


@dataclass(frozen=True, slots=True)
class _PreparedChunk:
    """等待写入事务的分块和向量。"""

    chunk_id: str
    draft: RepositoryChunkDraft
    embedding: tuple[float, ...]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _chunk_id(project_id: str, draft: RepositoryChunkDraft) -> str:
    """根据项目、位置和内容生成稳定分块标识。"""

    payload = (
        f"{project_id}\0{draft.path}\0{draft.start_line}\0"
        f"{draft.end_line}\0{draft.content_hash}"
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def build_fts_or_query(query: str) -> str | None:
    """把自由文本转换为不含 FTS 运算符的 OR 查询。"""

    expanded = _CAMEL_BOUNDARY.sub(" ", query)
    terms: list[str] = []
    raw_tokens = [match.group(0) for match in _SEARCH_TOKEN_PATTERN.finditer(query)]
    split_tokens = [match.group(0) for match in _SEARCH_TOKEN_PATTERN.finditer(expanded)]
    for raw_token in [*raw_tokens, *split_tokens]:
        token = raw_token.casefold()
        if token in _LEXICAL_QUERY_STOP_WORDS:
            continue
        if token not in terms:
            terms.append(token)
        if any("\u3400" <= char <= "\u9fff" for char in token) and len(token) > 1:
            for index in range(len(token) - 1):
                pair = token[index : index + 2]
                if pair not in terms:
                    terms.append(pair)
    if not terms:
        return None
    escaped = [f'"{term.replace(chr(34), chr(34) * 2)}"' for term in terms]
    return " OR ".join(escaped[:32])


def _cosine(left: Sequence[float], right: Sequence[float]) -> float:
    """计算余弦相似度，并安全处理零向量。"""

    if len(left) != len(right):
        raise RAGEmbeddingMismatchError("查询向量与索引向量维度不同")
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if not left_norm or not right_norm:
        return 0.0
    return dot / (left_norm * right_norm)


class SQLiteRAGIndex:
    """按 project_id 隔离并绑定 repo revision 的轻量混合索引。"""

    def __init__(
        self,
        storage_path: str | Path,
        embedding_client: EmbeddingClient,
        *,
        chunker: RepositoryChunker | None = None,
        search_config: HybridSearchConfig | None = None,
    ) -> None:
        self.storage_path = Path(storage_path).expanduser().resolve()
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)
        self.embedding_client = embedding_client
        self.chunker = chunker or RepositoryChunker()
        self.search_config = search_config or HybridSearchConfig()
        self._connection = sqlite3.connect(self.storage_path)
        self._connection.row_factory = sqlite3.Row
        self._initialize_schema()

    def close(self) -> None:
        """关闭 SQLite 连接。"""

        self._connection.close()

    def __enter__(self) -> "SQLiteRAGIndex":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _initialize_schema(self) -> None:
        """创建原文、向量和 FTS5 索引表。"""

        self._connection.executescript(
            """
            PRAGMA foreign_keys = ON;
            CREATE TABLE IF NOT EXISTS rag_index_state (
                project_id TEXT PRIMARY KEY,
                repo_revision TEXT NOT NULL,
                embedding_model TEXT NOT NULL,
                embedding_dimensions INTEGER NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS rag_documents (
                project_id TEXT NOT NULL,
                path TEXT NOT NULL,
                file_hash TEXT NOT NULL,
                repo_revision TEXT NOT NULL,
                chunk_count INTEGER NOT NULL,
                PRIMARY KEY (project_id, path)
            );
            CREATE TABLE IF NOT EXISTS rag_chunks (
                chunk_id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                path TEXT NOT NULL,
                kind TEXT NOT NULL,
                language TEXT NOT NULL,
                start_line INTEGER NOT NULL,
                end_line INTEGER NOT NULL,
                content TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                symbol TEXT,
                heading_path_json TEXT NOT NULL,
                embedding_json TEXT NOT NULL,
                embedding_model TEXT NOT NULL,
                embedding_dimensions INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_rag_chunks_project
                ON rag_chunks(project_id);
            CREATE INDEX IF NOT EXISTS idx_rag_chunks_project_path
                ON rag_chunks(project_id, path);
            CREATE VIRTUAL TABLE IF NOT EXISTS rag_chunks_fts USING fts5(
                chunk_id UNINDEXED,
                project_id UNINDEXED,
                path,
                content,
                structure
            );
            """
        )
        self._connection.commit()

    def index_repository(self, context: ProjectContext) -> IndexingReport:
        """按内容哈希增量更新索引，并在模型变化时完整重建。"""

        if inspect_repository(context.repo_root).revision != context.revision:
            raise RAGRevisionMismatchError(
                "传入的 ProjectContext 已过期，拒绝用旧版本标记新索引"
            )
        scan = self.chunker.scan(context)
        current_files = {item.path: item for item in scan.files}
        document_rows = self._connection.execute(
            """
            SELECT path, file_hash
            FROM rag_documents
            WHERE project_id = ?
            """,
            (context.project_id,),
        ).fetchall()
        existing_hashes = {str(row["path"]): str(row["file_hash"]) for row in document_rows}
        state = self._connection.execute(
            """
            SELECT embedding_model, embedding_dimensions
            FROM rag_index_state
            WHERE project_id = ?
            """,
            (context.project_id,),
        ).fetchone()
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
        embeddings = self.embedding_client.embed_texts(
            tuple(draft.embedding_text() for draft in all_drafts)
        )
        if len(embeddings) != len(all_drafts):
            raise RAGEmbeddingMismatchError("Embedding 返回数量与分块数量不一致")
        if any(len(vector) != self.embedding_client.dimensions for vector in embeddings):
            raise RAGEmbeddingMismatchError("Embedding 返回维度与客户端声明不一致")
        prepared = [
            _PreparedChunk(
                chunk_id=_chunk_id(context.project_id, draft),
                draft=draft,
                embedding=tuple(vector),
            )
            for draft, vector in zip(all_drafts, embeddings, strict=True)
        ]
        if inspect_repository(context.repo_root).revision != context.revision:
            raise RAGRevisionMismatchError(
                "代码库在索引过程中发生变化，本次结果未写入"
            )

        affected_paths = sorted(set(deleted_paths) | set(changed_paths))
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            for path in affected_paths:
                old_ids = self._connection.execute(
                    """
                    SELECT chunk_id FROM rag_chunks
                    WHERE project_id = ? AND path = ?
                    """,
                    (context.project_id, path),
                ).fetchall()
                for row in old_ids:
                    self._connection.execute(
                        "DELETE FROM rag_chunks_fts WHERE chunk_id = ?",
                        (row["chunk_id"],),
                    )
                self._connection.execute(
                    "DELETE FROM rag_chunks WHERE project_id = ? AND path = ?",
                    (context.project_id, path),
                )
                self._connection.execute(
                    "DELETE FROM rag_documents WHERE project_id = ? AND path = ?",
                    (context.project_id, path),
                )

            for item in prepared:
                draft = item.draft
                heading_json = json.dumps(draft.heading_path, ensure_ascii=False)
                self._connection.execute(
                    """
                    INSERT INTO rag_chunks (
                        chunk_id, project_id, path, kind, language,
                        start_line, end_line, content, content_hash, symbol,
                        heading_path_json, embedding_json, embedding_model,
                        embedding_dimensions
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        item.chunk_id,
                        context.project_id,
                        draft.path,
                        draft.kind,
                        draft.language,
                        draft.start_line,
                        draft.end_line,
                        draft.content,
                        draft.content_hash,
                        draft.symbol,
                        heading_json,
                        json.dumps(item.embedding, separators=(",", ":")),
                        self.embedding_client.model_id,
                        self.embedding_client.dimensions,
                    ),
                )
                structure = " ".join(
                    part
                    for part in [draft.symbol, *draft.heading_path]
                    if part
                )
                self._connection.execute(
                    """
                    INSERT INTO rag_chunks_fts (
                        chunk_id, project_id, path, content, structure
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        item.chunk_id,
                        context.project_id,
                        draft.path,
                        draft.content,
                        structure,
                    ),
                )

            for path in changed_paths:
                source = current_files[path]
                self._connection.execute(
                    """
                    INSERT INTO rag_documents (
                        project_id, path, file_hash, repo_revision, chunk_count
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        context.project_id,
                        path,
                        source.content_hash,
                        context.revision,
                        len(drafts_by_path[path]),
                    ),
                )
            self._connection.execute(
                """
                UPDATE rag_documents SET repo_revision = ?
                WHERE project_id = ?
                """,
                (context.revision, context.project_id),
            )
            self._connection.execute(
                """
                INSERT INTO rag_index_state (
                    project_id, repo_revision, embedding_model,
                    embedding_dimensions, updated_at
                ) VALUES (?, ?, ?, ?, ?)
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
                    _utc_now(),
                ),
            )
            self._connection.commit()
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

    def _validate_state(self, context: ProjectContext) -> None:
        """检索前确保项目、版本和向量空间完全一致。"""

        state = self._connection.execute(
            """
            SELECT repo_revision, embedding_model, embedding_dimensions
            FROM rag_index_state WHERE project_id = ?
            """,
            (context.project_id,),
        ).fetchone()
        if state is None:
            raise RAGIndexNotReadyError(
                f"项目尚未建立 RAG 索引：{context.project_id}"
            )
        if state["repo_revision"] != context.revision:
            raise RAGRevisionMismatchError(
                "RAG 索引版本与当前代码库版本不一致，必须先增量更新"
            )
        if (
            state["embedding_model"] != self.embedding_client.model_id
            or state["embedding_dimensions"] != self.embedding_client.dimensions
        ):
            raise RAGEmbeddingMismatchError(
                "RAG 索引使用了不同的 Embedding 模型或维度，必须重建"
            )

    def _lexical_rank(
        self,
        project_id: str,
        query: str,
        limit: int,
    ) -> list[str]:
        """使用 FTS5 BM25 获取关键词候选。"""

        match_query = build_fts_or_query(query)
        if match_query is None:
            return []
        rows = self._connection.execute(
            """
            SELECT chunk_id, bm25(rag_chunks_fts, 0.0, 0.0, 0.3, 1.0, 2.0) AS rank_score
            FROM rag_chunks_fts
            WHERE rag_chunks_fts MATCH ? AND project_id = ?
            ORDER BY rank_score ASC
            LIMIT ?
            """,
            (match_query, project_id, limit),
        ).fetchall()
        return [str(row["chunk_id"]) for row in rows]

    def _dense_rank(
        self,
        project_id: str,
        query: str,
        limit: int,
    ) -> list[str]:
        """对小型本地索引执行精确余弦扫描。"""

        rows = self._connection.execute(
            """
            SELECT chunk_id, embedding_json
            FROM rag_chunks
            WHERE project_id = ?
            ORDER BY chunk_id
            LIMIT ?
            """,
            (project_id, self.search_config.max_dense_scan_chunks + 1),
        ).fetchall()
        if len(rows) > self.search_config.max_dense_scan_chunks:
            raise RAGIndexError(
                "本地向量精确扫描超过分块上限，应切换专业向量数据库"
            )
        query_vectors = self.embedding_client.embed_texts((query,))
        if len(query_vectors) != 1:
            raise RAGEmbeddingMismatchError("查询 Embedding 未返回一个向量")
        query_vector = query_vectors[0]
        if not any(query_vector):
            return []
        scored: list[tuple[str, float]] = []
        for row in rows:
            vector = tuple(float(value) for value in json.loads(row["embedding_json"]))
            score = _cosine(query_vector, vector)
            if score > 0:
                scored.append((str(row["chunk_id"]), score))
        scored.sort(key=lambda item: (-item[1], item[0]))
        return [chunk_id for chunk_id, _ in scored[:limit]]

    def search(
        self,
        context: ProjectContext,
        query: str,
        *,
        top_k: int = 5,
        mode: Literal["hybrid", "lexical", "dense"] = "hybrid",
    ) -> RetrievalResult:
        """执行 BM25、向量或 RRF 混合检索并返回可复核引用。"""

        normalized_query = query.strip()
        if not normalized_query or len(normalized_query) > 2_000:
            raise ValueError("query 长度必须为 1 到 2000")
        if not 1 <= top_k <= 20:
            raise ValueError("top_k 必须在 1 到 20 之间")
        if mode not in {"hybrid", "lexical", "dense"}:
            raise ValueError("mode 必须是 hybrid、lexical 或 dense")
        self._validate_state(context)

        pool_size = top_k * self.search_config.candidate_pool_multiplier
        lexical_ids = (
            self._lexical_rank(context.project_id, normalized_query, pool_size)
            if mode in {"hybrid", "lexical"}
            else []
        )
        dense_ids = (
            self._dense_rank(context.project_id, normalized_query, pool_size)
            if mode in {"hybrid", "dense"}
            else []
        )
        lexical_ranks = {chunk_id: rank for rank, chunk_id in enumerate(lexical_ids, 1)}
        dense_ranks = {chunk_id: rank for rank, chunk_id in enumerate(dense_ids, 1)}
        candidates = set(lexical_ranks) | set(dense_ranks)
        fused: list[tuple[str, float]] = []
        for chunk_id in candidates:
            score = 0.0
            if chunk_id in lexical_ranks:
                score += self.search_config.lexical_weight / (
                    self.search_config.rrf_k + lexical_ranks[chunk_id]
                )
            if chunk_id in dense_ranks:
                score += self.search_config.dense_weight / (
                    self.search_config.rrf_k + dense_ranks[chunk_id]
                )
            fused.append((chunk_id, score))
        fused.sort(key=lambda item: (-item[1], item[0]))
        selected = fused[:top_k]

        hits: list[RetrievalHit] = []
        for chunk_id, score in selected:
            row = self._connection.execute(
                "SELECT * FROM rag_chunks WHERE chunk_id = ?",
                (chunk_id,),
            ).fetchone()
            if row is None:
                continue
            path = str(row["path"])
            start_line = int(row["start_line"])
            end_line = int(row["end_line"])
            hits.append(
                RetrievalHit(
                    chunk_id=chunk_id,
                    path=path,
                    kind=str(row["kind"]),
                    language=str(row["language"]),
                    start_line=start_line,
                    end_line=end_line,
                    content=str(row["content"]),
                    content_hash=str(row["content_hash"]),
                    symbol=(str(row["symbol"]) if row["symbol"] else None),
                    heading_path=tuple(json.loads(row["heading_path_json"])),
                    citation=f"{path}:{start_line}-{end_line}",
                    score=score,
                    lexical_rank=lexical_ranks.get(chunk_id),
                    dense_rank=dense_ranks.get(chunk_id),
                )
            )
        return RetrievalResult(
            project_id=context.project_id,
            repo_revision=context.revision,
            query=normalized_query,
            hits=tuple(hits),
            lexical_candidates=len(lexical_ids),
            dense_candidates=len(dense_ids),
            embedding_model=self.embedding_client.model_id,
        )

    def count_chunks(self, project_id: str) -> int:
        """返回项目分块数量，供审计和测试使用。"""

        row = self._connection.execute(
            "SELECT COUNT(*) AS total FROM rag_chunks WHERE project_id = ?",
            (project_id,),
        ).fetchone()
        return int(row["total"])

    def delete_project(self, project_id: str) -> None:
        """删除一个项目的索引状态、文件、分块和 FTS 记录。"""

        try:
            self._connection.execute("BEGIN IMMEDIATE")
            chunk_rows = self._connection.execute(
                "SELECT chunk_id FROM rag_chunks WHERE project_id = ?",
                (project_id,),
            ).fetchall()
            for row in chunk_rows:
                self._connection.execute(
                    "DELETE FROM rag_chunks_fts WHERE chunk_id = ?",
                    (row["chunk_id"],),
                )
            self._connection.execute(
                "DELETE FROM rag_chunks WHERE project_id = ?",
                (project_id,),
            )
            self._connection.execute(
                "DELETE FROM rag_documents WHERE project_id = ?",
                (project_id,),
            )
            self._connection.execute(
                "DELETE FROM rag_index_state WHERE project_id = ?",
                (project_id,),
            )
            self._connection.commit()
        except Exception:
            self._connection.rollback()
            raise
