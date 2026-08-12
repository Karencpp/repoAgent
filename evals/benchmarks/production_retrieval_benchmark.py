"""Evaluate production GLM embeddings on the stratified Django core Qrels."""

from __future__ import annotations

import argparse
from collections import defaultdict
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import statistics
import struct
import time
from typing import Any, Sequence

from repo_agent.projects import ProjectContextResolver, ProjectRegistry
from repo_agent.rag import GLMEmbeddingClient, GLMEmbeddingConfig, PostgresRAGIndex


EXPECTED_COMMIT = "c9eb16a87e60c305fb3651459639f647cce498db"


class PersistentCachingEmbeddingClient:
    """Persist real embeddings after every API batch so interrupted runs can resume."""

    def __init__(self, delegate: GLMEmbeddingClient, cache_path: Path) -> None:
        self.delegate = delegate
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(cache_path)
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS embeddings (
                cache_key TEXT PRIMARY KEY,
                vector BLOB NOT NULL
            )
            """
        )
        self.cache_hit_text_count = 0
        self.embedded_text_count = 0
        self.api_request_count = 0

    @property
    def model_id(self) -> str:
        return self.delegate.model_id

    @property
    def dimensions(self) -> int:
        return self.delegate.dimensions

    def close(self) -> None:
        self.connection.close()

    def _key(self, text: str) -> str:
        payload = f"{self.model_id}\0{text}".encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    def _decode(self, payload: bytes) -> tuple[float, ...]:
        expected = self.dimensions * 4
        if len(payload) != expected:
            raise RuntimeError(
                f"Embedding cache entry has {len(payload)} bytes; expected {expected}"
            )
        return struct.unpack(f"<{self.dimensions}f", payload)

    def _encode(self, vector: Sequence[float]) -> bytes:
        if len(vector) != self.dimensions:
            raise RuntimeError("Embedding cache received a vector with wrong dimensions")
        return struct.pack(f"<{self.dimensions}f", *vector)

    def embed_texts(
        self, texts: Sequence[str]
    ) -> tuple[tuple[float, ...], ...]:
        values = tuple(texts)
        if not values:
            return ()

        positions_by_key: dict[str, list[int]] = defaultdict(list)
        text_by_key: dict[str, str] = {}
        for position, value in enumerate(values):
            key = self._key(value)
            positions_by_key[key].append(position)
            text_by_key.setdefault(key, value)

        vectors_by_key: dict[str, tuple[float, ...]] = {}
        for key in positions_by_key:
            row = self.connection.execute(
                "SELECT vector FROM embeddings WHERE cache_key = ?", (key,)
            ).fetchone()
            if row is not None:
                vectors_by_key[key] = self._decode(row[0])
                self.cache_hit_text_count += len(positions_by_key[key])

        missing_keys = [key for key in positions_by_key if key not in vectors_by_key]
        batch_size = self.delegate.config.batch_size
        total_batches = (len(missing_keys) + batch_size - 1) // batch_size
        for offset in range(0, len(missing_keys), batch_size):
            batch_keys = missing_keys[offset : offset + batch_size]
            batch_vectors = self.delegate.embed_texts(
                tuple(text_by_key[key] for key in batch_keys)
            )
            self.api_request_count += 1
            self.embedded_text_count += len(batch_keys)
            rows = []
            for key, vector in zip(batch_keys, batch_vectors, strict=True):
                normalized = tuple(vector)
                vectors_by_key[key] = normalized
                rows.append((key, self._encode(normalized)))
            self.connection.executemany(
                "INSERT OR REPLACE INTO embeddings(cache_key, vector) VALUES (?, ?)",
                rows,
            )
            self.connection.commit()
            batch_number = offset // batch_size + 1
            if total_batches > 1 and (
                batch_number % 10 == 0 or batch_number == total_batches
            ):
                print(
                    f"  embedding API batches: {batch_number}/{total_batches}",
                    flush=True,
                )

        ordered: list[tuple[float, ...] | None] = [None] * len(values)
        for key, positions in positions_by_key.items():
            vector = vectors_by_key[key]
            for position in positions:
                ordered[position] = vector
        if any(vector is None for vector in ordered):
            raise RuntimeError("Embedding cache failed to resolve every input")
        return tuple(vector for vector in ordered if vector is not None)


def _load_cases(path: Path) -> tuple[dict[str, Any], ...]:
    cases = tuple(
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    )
    if not cases:
        raise ValueError("Retrieval dataset is empty")
    return cases


def _metrics(details: list[dict[str, Any]], top_k: int) -> dict[str, float]:
    recalls = []
    reciprocal_ranks = []
    for detail in details:
        retrieved = detail["retrieved_paths"][:top_k]
        relevant = set(detail["relevant_paths"])
        recalls.append(len(relevant.intersection(retrieved)) / len(relevant))
        rank = next(
            (index for index, path in enumerate(retrieved, 1) if path in relevant),
            None,
        )
        reciprocal_ranks.append(1.0 / rank if rank else 0.0)
    return {
        f"recall_at_{top_k}": statistics.fmean(recalls),
        f"mrr_at_{top_k}": statistics.fmean(reciprocal_ranks),
        f"hit_rate_at_{top_k}": sum(value > 0 for value in reciprocal_ranks)
        / len(reciprocal_ranks),
    }


def _evaluate(index, context, cases, mode: str) -> dict[str, Any]:
    details = []
    durations = []
    for position, case in enumerate(cases, 1):
        started = time.perf_counter()
        result = index.search(context, case["query"], top_k=10, mode=mode)
        durations.append((time.perf_counter() - started) * 1_000)
        details.append(
            {
                "case_id": case["case_id"],
                "category": case["category"],
                "relevant_paths": case["relevant_paths"],
                "retrieved_paths": [hit.path for hit in result.hits],
            }
        )
        if position % 10 == 0 or position == len(cases):
            print(f"  {mode}: {position}/{len(cases)}", flush=True)

    by_category: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for detail in details:
        by_category[detail["category"]].append(detail)
    ordered = sorted(durations)
    return {
        "overall": {**_metrics(details, 5), **_metrics(details, 10)},
        "by_category": {
            category: {
                "case_count": len(category_details),
                **_metrics(category_details, 5),
                **_metrics(category_details, 10),
            }
            for category, category_details in sorted(by_category.items())
        },
        "latency_ms": {
            "samples": len(durations),
            "p50": ordered[round((len(ordered) - 1) * 0.50)],
            "p95": ordered[round((len(ordered) - 1) * 0.95)],
            "mean": statistics.fmean(durations),
        },
        "cases": details,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    cases = _load_cases(arguments.dataset)
    state_dir = arguments.output.parent / "state" / "production-retrieval"
    state_dir.mkdir(parents=True, exist_ok=True)
    context = ProjectContextResolver(
        ProjectRegistry(state_dir / "projects.json")
    ).resolve(repo=arguments.repo)
    if context.commit_sha != EXPECTED_COMMIT or context.is_dirty:
        raise RuntimeError(
            f"Expected clean Django {EXPECTED_COMMIT}, got {context.revision}"
        )

    config = GLMEmbeddingConfig.from_env()
    if config.dimensions != 512:
        raise RuntimeError("Production benchmark is pinned to 512 dimensions")
    delegate = GLMEmbeddingClient(config)
    embedding = PersistentCachingEmbeddingClient(
        delegate,
        state_dir / "glm-embedding-3-512.sqlite3",
    )
    index = PostgresRAGIndex(os.environ["REPO_AGENT_POSTGRES_DSN"], embedding)
    try:
        print("[1/4] Indexing Django core with GLM embedding-3", flush=True)
        started = time.perf_counter()
        indexing = index.index_repository(context)
        indexing_seconds = time.perf_counter() - started
        with index._connection.cursor() as cursor:
            cursor.execute("ANALYZE repository_chunks")
            chunk_count = cursor.execute(
                "SELECT count(*) AS total FROM repository_chunks WHERE project_id = %s",
                (context.project_id,),
            ).fetchone()["total"]

        print("[2/4] Precomputing query vectors", flush=True)
        started = time.perf_counter()
        embedding.embed_texts(tuple(case["query"] for case in cases))
        query_embedding_seconds = time.perf_counter() - started

        print("[3/4] Evaluating lexical, dense, and hybrid retrieval", flush=True)
        quality = {
            mode: _evaluate(index, context, cases, mode)
            for mode in ("lexical", "dense", "hybrid")
        }
        print("[4/4] Writing report", flush=True)
        report = {
            "schema": "repo-agent-production-retrieval-v1",
            "repository": {
                "name": "django/django core source",
                "commit": context.commit_sha,
                "files": indexing.scanned_files,
                "chunks": chunk_count,
            },
            "dataset": {
                "path": str(arguments.dataset),
                "case_count": len(cases),
            },
            "embedding": {
                "model_id": embedding.model_id,
                "dimensions": embedding.dimensions,
                "newly_embedded_text_count": embedding.embedded_text_count,
                "cache_hit_text_count": embedding.cache_hit_text_count,
                "api_request_count": embedding.api_request_count,
                "query_precompute_seconds": query_embedding_seconds,
            },
            "indexing": {
                "seconds": indexing_seconds,
                "indexed_files": indexing.indexed_files,
                "written_chunks": indexing.written_chunks,
            },
            "quality": quality,
        }
        arguments.output.parent.mkdir(parents=True, exist_ok=True)
        arguments.output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(json.dumps(report, ensure_ascii=False, indent=2))
    finally:
        index.close()
        embedding.close()
        delegate.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
