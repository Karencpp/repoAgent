"""Evaluate GLM Rerank over cached production Hybrid candidates."""

from __future__ import annotations

import argparse
from collections import defaultdict
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import statistics
import time
from typing import Any, Sequence

from repo_agent.llm.contracts import LLMResponseError
from repo_agent.projects import ProjectContextResolver, ProjectRegistry
from repo_agent.rag import (
    GLMEmbeddingClient,
    GLMEmbeddingConfig,
    GLMRerankerClient,
    GLMRerankerConfig,
    PostgresRAGIndex,
)

from evals.benchmarks.production_retrieval_benchmark import (
    EXPECTED_COMMIT,
    PersistentCachingEmbeddingClient,
)


def _load_cases(path: Path) -> tuple[dict[str, Any], ...]:
    cases = tuple(
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    )
    if not cases:
        raise ValueError("Reranker dataset is empty")
    return cases


def _unique_paths(paths: Sequence[str], top_k: int) -> tuple[str, ...]:
    return tuple(dict.fromkeys(paths))[:top_k]


def _metrics(details: Sequence[dict[str, Any]], key: str, top_k: int) -> dict[str, float]:
    recalls = []
    reciprocal_ranks = []
    ndcgs = []
    for detail in details:
        retrieved = _unique_paths(detail[key], top_k)
        relevant = set(detail["relevant_paths"])
        recalls.append(len(relevant.intersection(retrieved)) / len(relevant))
        rank = next(
            (position for position, path in enumerate(retrieved, 1) if path in relevant),
            None,
        )
        reciprocal_ranks.append(1.0 / rank if rank else 0.0)
        dcg = sum(
            1.0 / math.log2(position + 1)
            for position, path in enumerate(retrieved, 1)
            if path in relevant
        )
        ideal_count = min(len(relevant), top_k)
        ideal = sum(1.0 / math.log2(position + 1) for position in range(1, ideal_count + 1))
        ndcgs.append(dcg / ideal if ideal else 0.0)
    return {
        f"recall_at_{top_k}": statistics.fmean(recalls),
        f"mrr_at_{top_k}": statistics.fmean(reciprocal_ranks),
        f"ndcg_at_{top_k}": statistics.fmean(ndcgs),
        f"hit_rate_at_{top_k}": sum(value > 0 for value in reciprocal_ranks)
        / len(reciprocal_ranks),
    }


def _summarize(details: Sequence[dict[str, Any]], key: str) -> dict[str, Any]:
    by_category: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for detail in details:
        by_category[detail["category"]].append(detail)
    return {
        "overall": {**_metrics(details, key, 5), **_metrics(details, key, 10)},
        "by_category": {
            category: {
                "case_count": len(category_details),
                **_metrics(category_details, key, 5),
                **_metrics(category_details, key, 10),
            }
            for category, category_details in sorted(by_category.items())
        },
    }


def _document(row: dict[str, Any], max_chars: int) -> str:
    header = (
        f"path: {row['path']}\n"
        f"symbol: {row['symbol'] or ''}\n"
        f"kind: {row['kind']}\n"
        "content:\n"
    )
    return (header + str(row["content"]))[:max_chars]


def _hybrid_candidates(index, context, query: str, candidate_k: int):
    pool_size = candidate_k * index.search_config.candidate_pool_multiplier
    lexical_ids, dense_ids = index._rank_ids(context, query, pool_size, "hybrid")
    lexical_ranks = {chunk_id: rank for rank, chunk_id in enumerate(lexical_ids, 1)}
    dense_ranks = {chunk_id: rank for rank, chunk_id in enumerate(dense_ids, 1)}
    fused = []
    for chunk_id in set(lexical_ranks) | set(dense_ranks):
        score = 0.0
        if chunk_id in lexical_ranks:
            score += index.search_config.lexical_weight / (
                index.search_config.rrf_k + lexical_ranks[chunk_id]
            )
        if chunk_id in dense_ranks:
            score += index.search_config.dense_weight / (
                index.search_config.rrf_k + dense_ranks[chunk_id]
            )
        fused.append((chunk_id, score))
    fused.sort(key=lambda item: (-item[1], item[0]))
    selected = fused[:candidate_k]
    if not selected:
        return ()
    with index._connection.cursor() as cursor:
        rows = cursor.execute(
            "SELECT * FROM repository_chunks WHERE chunk_id = ANY(%s)",
            ([chunk_id for chunk_id, _ in selected],),
        ).fetchall()
    by_id = {str(row["chunk_id"]): dict(row) for row in rows}
    return tuple(by_id[chunk_id] for chunk_id, _ in selected if chunk_id in by_id)


class PersistentRerankCache:
    """Cache one relevance score per query/document/model tuple."""

    def __init__(
        self,
        delegate: GLMRerankerClient,
        path: Path,
        *,
        max_batch_documents: int = 8,
        max_batch_chars: int = 16_000,
    ) -> None:
        if max_batch_documents < 1 or max_batch_chars < 1:
            raise ValueError("Rerank batch limits must be positive")
        self.delegate = delegate
        self.max_batch_documents = max_batch_documents
        self.max_batch_chars = max_batch_chars
        self.connection = sqlite3.connect(path)
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS rerank_scores (
                cache_key TEXT PRIMARY KEY,
                relevance_score REAL NOT NULL
            )
            """
        )
        self.api_request_count = 0
        self.rejected_request_count = 0
        self.cache_hit_document_count = 0
        self.prompt_tokens = 0
        self.total_tokens = 0
        self.request_durations_ms: list[float] = []

    @property
    def model_id(self) -> str:
        return self.delegate.model_id

    def close(self) -> None:
        self.connection.close()

    def _key(self, query: str, document: str) -> str:
        payload = f"{self.model_id}\0{query}\0{document}".encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    def scores(self, query: str, documents: Sequence[str]) -> tuple[float, ...]:
        keys = [self._key(query, document) for document in documents]
        resolved: list[float | None] = []
        missing_positions = []
        for position, key in enumerate(keys):
            row = self.connection.execute(
                "SELECT relevance_score FROM rerank_scores WHERE cache_key = ?",
                (key,),
            ).fetchone()
            if row is None:
                resolved.append(None)
                missing_positions.append(position)
            else:
                resolved.append(float(row[0]))
                self.cache_hit_document_count += 1

        def persist_batch(batch_positions: Sequence[int]) -> None:
            batch_documents = tuple(documents[position] for position in batch_positions)
            started = time.perf_counter()
            try:
                response = self.delegate.rerank(query, batch_documents)
            except LLMResponseError as exc:
                self.rejected_request_count += 1
                if "query+document" not in str(exc) or len(batch_positions) == 1:
                    raise
                midpoint = len(batch_positions) // 2
                persist_batch(batch_positions[:midpoint])
                persist_batch(batch_positions[midpoint:])
                return
            self.request_durations_ms.append((time.perf_counter() - started) * 1_000)
            self.api_request_count += 1
            self.prompt_tokens += response.prompt_tokens
            self.total_tokens += response.total_tokens
            rows = []
            for score in response.scores:
                original_position = batch_positions[score.index]
                resolved[original_position] = score.relevance_score
                rows.append((keys[original_position], score.relevance_score))
            self.connection.executemany(
                "INSERT OR REPLACE INTO rerank_scores(cache_key, relevance_score) VALUES (?, ?)",
                rows,
            )
            self.connection.commit()

        batch: list[int] = []
        batch_chars = 0
        for position in missing_positions:
            pair_chars = len(query) + len(documents[position])
            if batch and (
                len(batch) >= self.max_batch_documents
                or batch_chars + pair_chars > self.max_batch_chars
            ):
                persist_batch(tuple(batch))
                batch = []
                batch_chars = 0
            batch.append(position)
            batch_chars += pair_chars
        if batch:
            persist_batch(tuple(batch))

        if any(score is None for score in resolved):
            raise RuntimeError("Rerank cache failed to resolve every document")
        return tuple(float(score) for score in resolved if score is not None)


def _percentile(values: Sequence[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[round((len(ordered) - 1) * percentile)]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--candidate-k", type=int, default=40)
    arguments = parser.parse_args()
    if not 10 <= arguments.candidate_k <= 128:
        raise ValueError("candidate-k must be between 10 and 128")

    cases = _load_cases(arguments.dataset)
    state_dir = arguments.output.parent / "state" / "production-retrieval"
    context = ProjectContextResolver(
        ProjectRegistry(state_dir / "projects.json")
    ).resolve(repo=arguments.repo)
    if context.commit_sha != EXPECTED_COMMIT or context.is_dirty:
        raise RuntimeError(f"Expected clean Django {EXPECTED_COMMIT}, got {context.revision}")

    embedding_delegate = GLMEmbeddingClient(GLMEmbeddingConfig.from_env())
    embedding = PersistentCachingEmbeddingClient(
        embedding_delegate,
        state_dir / "glm-embedding-3-512.sqlite3",
    )
    rerank_delegate = GLMRerankerClient(GLMRerankerConfig.from_env())
    reranker = PersistentRerankCache(
        rerank_delegate,
        state_dir / "glm-rerank.sqlite3",
    )
    index = PostgresRAGIndex(os.environ["REPO_AGENT_POSTGRES_DSN"], embedding)
    details = []
    try:
        for position, case in enumerate(cases, 1):
            rows = _hybrid_candidates(
                index,
                context,
                case["query"],
                arguments.candidate_k,
            )
            documents = tuple(
                _document(
                    row,
                    min(
                        rerank_delegate.config.max_document_chars,
                        rerank_delegate.config.max_pair_chars
                        - len(case["query"]),
                    ),
                )
                for row in rows
            )
            scores = reranker.scores(case["query"], documents)
            ranked_40 = sorted(
                range(len(rows)),
                key=lambda item: (-scores[item], item),
            )
            first_20 = range(min(20, len(rows)))
            ranked_20 = sorted(first_20, key=lambda item: (-scores[item], item))
            paths = [str(row["path"]) for row in rows]
            details.append(
                {
                    "case_id": case["case_id"],
                    "category": case["category"],
                    "relevant_paths": case["relevant_paths"],
                    "hybrid_paths": paths[:10],
                    "candidate_paths_20": paths[:20],
                    "candidate_paths_40": paths,
                    "rerank_paths_20": [paths[item] for item in ranked_20[:10]],
                    "rerank_paths_40": [paths[item] for item in ranked_40[:10]],
                }
            )
            if position % 10 == 0 or position == len(cases):
                print(
                    f"rerank: {position}/{len(cases)} "
                    f"(API requests: {reranker.api_request_count})",
                    flush=True,
                )

        report = {
            "schema": "repo-agent-reranker-api-v1",
            "repository": {
                "name": "django/django core source",
                "commit": context.commit_sha,
            },
            "dataset": {"case_count": len(cases)},
            "candidate_k": arguments.candidate_k,
            "embedding": {
                "model_id": embedding.model_id,
                "api_request_count": embedding.api_request_count,
            },
            "reranker": {
                "model_id": reranker.model_id,
                "api_request_count": reranker.api_request_count,
                "rejected_request_count": reranker.rejected_request_count,
                "cache_hit_document_count": reranker.cache_hit_document_count,
                "prompt_tokens": reranker.prompt_tokens,
                "total_tokens": reranker.total_tokens,
                "batching": {
                    "max_documents": reranker.max_batch_documents,
                    "max_pair_chars": rerank_delegate.config.max_pair_chars,
                    "max_batch_chars": reranker.max_batch_chars,
                },
                "latency_ms": {
                    "samples": len(reranker.request_durations_ms),
                    "p50": _percentile(reranker.request_durations_ms, 0.50),
                    "p95": _percentile(reranker.request_durations_ms, 0.95),
                    "mean": (
                        statistics.fmean(reranker.request_durations_ms)
                        if reranker.request_durations_ms
                        else None
                    ),
                },
            },
            "quality": {
                "hybrid": _summarize(details, "hybrid_paths"),
                "candidate_upper_bound_20": _summarize(details, "candidate_paths_20"),
                "candidate_upper_bound_40": _summarize(details, "candidate_paths_40"),
                "rerank_20": _summarize(details, "rerank_paths_20"),
                "rerank_40": _summarize(details, "rerank_paths_40"),
            },
            "cases": details,
        }
        arguments.output.parent.mkdir(parents=True, exist_ok=True)
        arguments.output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(json.dumps(report["reranker"], ensure_ascii=False, indent=2))
        print(json.dumps(report["quality"], ensure_ascii=False, indent=2))
    finally:
        index.close()
        reranker.close()
        rerank_delegate.close()
        embedding.close()
        embedding_delegate.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
