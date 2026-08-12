"""Benchmark SQLite exact search, pgvector HNSW, and context budgeting."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import os
from pathlib import Path
import statistics
import time
from typing import Any, Literal, Protocol

from repo_agent.context_engineering import (
    ContextBuilder,
    ContextBuilderConfig,
    packets_from_rag,
    system_packet,
    task_packet,
    tool_observation_packet,
    working_state_packet,
)
from repo_agent.projects import ProjectContextResolver, ProjectRegistry
from repo_agent.rag import (
    FeatureHashEmbeddingClient,
    HybridSearchConfig,
    PostgresRAGIndex,
    SQLiteRAGIndex,
)


EXPECTED_DJANGO_COMMIT = "c9eb16a87e60c305fb3651459639f647cce498db"


@dataclass(frozen=True, slots=True)
class RetrievalCase:
    case_id: str
    query: str
    relevant_paths: tuple[str, ...]


class SearchIndex(Protocol):
    def search(
        self,
        context,
        query: str,
        *,
        top_k: int,
        mode: Literal["hybrid", "lexical", "dense"],
    ): ...


def _load_cases(path: Path) -> tuple[RetrievalCase, ...]:
    cases = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        payload = json.loads(line)
        try:
            cases.append(
                RetrievalCase(
                    case_id=str(payload["case_id"]),
                    query=str(payload["query"]),
                    relevant_paths=tuple(str(item) for item in payload["relevant_paths"]),
                )
            )
        except (KeyError, TypeError) as exc:
            raise ValueError(f"Invalid case at {path}:{line_number}") from exc
    if not cases:
        raise ValueError("Benchmark dataset is empty")
    return tuple(cases)


def _percentile(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    index = round((len(ordered) - 1) * percentile)
    return ordered[index]


def _quality(index: SearchIndex, context, cases, mode: str, top_k: int) -> dict[str, Any]:
    recalls = []
    reciprocal_ranks = []
    details = []
    for position, case in enumerate(cases, 1):
        result = index.search(context, case.query, top_k=top_k, mode=mode)
        retrieved = tuple(hit.path for hit in result.hits)
        relevant = set(case.relevant_paths)
        recalls.append(len(relevant.intersection(retrieved)) / len(relevant))
        first_rank = next(
            (rank for rank, path in enumerate(retrieved, 1) if path in relevant),
            None,
        )
        reciprocal_ranks.append(1.0 / first_rank if first_rank else 0.0)
        details.append(
            {
                "case_id": case.case_id,
                "retrieved_paths": retrieved,
                "first_relevant_rank": first_rank,
            }
        )
        if position % 10 == 0 or position == len(cases):
            print(f"  quality {mode}: {position}/{len(cases)}", flush=True)
    return {
        "top_k": top_k,
        "recall_at_k": statistics.fmean(recalls),
        "mrr": statistics.fmean(reciprocal_ranks),
        "hit_rate": sum(value > 0 for value in reciprocal_ranks) / len(cases),
        "cases": details,
    }


def _latency(
    index: SearchIndex,
    context,
    cases,
    mode: str,
    top_k: int,
    repetitions: int,
) -> dict[str, Any]:
    warmup_cases = cases[: min(5, len(cases))]
    for case in warmup_cases:
        index.search(context, case.query, top_k=top_k, mode=mode)
    durations = []
    completed = 0
    total = repetitions * len(cases)
    for _ in range(repetitions):
        for case in cases:
            started = time.perf_counter()
            index.search(context, case.query, top_k=top_k, mode=mode)
            durations.append((time.perf_counter() - started) * 1_000)
            completed += 1
            if completed % 10 == 0 or completed == total:
                print(f"  latency {mode}: {completed}/{total}", flush=True)
    return {
        "samples": len(durations),
        "p50_ms": _percentile(durations, 0.50),
        "p95_ms": _percentile(durations, 0.95),
        "p99_ms": _percentile(durations, 0.99),
        "mean_ms": statistics.fmean(durations),
    }


def _ann_recall(sqlite_index, postgres_index, context, cases, top_k: int) -> float:
    recalls = []
    for position, case in enumerate(cases, 1):
        exact = sqlite_index.search(context, case.query, top_k=top_k, mode="dense")
        approximate = postgres_index.search(
            context, case.query, top_k=top_k, mode="dense"
        )
        exact_ids = {hit.chunk_id for hit in exact.hits}
        approximate_ids = {hit.chunk_id for hit in approximate.hits}
        recalls.append(len(exact_ids.intersection(approximate_ids)) / len(exact_ids))
        if position % 10 == 0 or position == len(cases):
            print(f"  ANN recall: {position}/{len(cases)}", flush=True)
    return statistics.fmean(recalls)


def _find_index_names(value: Any) -> list[str]:
    found = []
    if isinstance(value, dict):
        if "Index Name" in value:
            found.append(str(value["Index Name"]))
        for child in value.values():
            found.extend(_find_index_names(child))
    elif isinstance(value, list):
        for child in value:
            found.extend(_find_index_names(child))
    return found


def _explain_hnsw(postgres_index, context, embedding, query: str) -> dict[str, Any]:
    vector = embedding.embed_texts((query,))[0]
    literal = "[" + ",".join(f"{value:.12g}" for value in vector) + "]"
    dimensions = embedding.dimensions
    vector_type = f"vector({dimensions})"
    with postgres_index._connection.cursor() as cursor:
        row = cursor.execute(
            f"""
            EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)
            SELECT chunk_id
            FROM repository_chunks
            WHERE project_id = %s AND repo_revision = %s
              AND embedding_model = %s AND embedding_dimensions = {dimensions}
            ORDER BY (embedding::{vector_type}) <=> %s::{vector_type}
            LIMIT 10
            """,
            (context.project_id, context.revision, embedding.model_id, literal),
        ).fetchone()
    plan = next(iter(row.values()))
    index_names = sorted(set(_find_index_names(plan)))
    return {
        "hnsw_index_used": any("hnsw" in name for name in index_names),
        "index_names": index_names,
        "plan": plan,
    }


def _context_metrics(postgres_index, context, cases) -> dict[str, Any]:
    baseline_builder = ContextBuilder(
        config=ContextBuilderConfig(
            model_context_window=1_000_000,
            reserved_output_tokens=0,
            enable_compression=False,
        )
    )
    budgeted_builder = ContextBuilder(
        config=ContextBuilderConfig(
            model_context_window=8_000,
            reserved_output_tokens=1_000,
            min_compression_target_tokens=32,
        )
    )
    raw_tokens = []
    budgeted_tokens = []
    relevant_retained = 0
    mandatory_retained = 0
    compression_count = 0
    details = []
    for position, case in enumerate(cases, 1):
        retrieval = postgres_index.search(
            context, case.query, top_k=20, mode="hybrid"
        )
        relevant_path = case.relevant_paths[0]
        relevant_packet_id = f"benchmark-relevant:{case.case_id}"
        relevant_source = (context.repo_root / relevant_path).read_text(
            encoding="utf-8-sig", errors="replace"
        )[:80_000]
        packets = [
            system_packet("Answer only from repository evidence."),
            task_packet(case.query),
            working_state_packet("Current stage: inspect retrieved implementation evidence."),
            tool_observation_packet(
                relevant_packet_id,
                relevant_source,
                citations=(relevant_path,),
                priority=92,
            ),
            *packets_from_rag(retrieval),
        ]
        seen_paths = set()
        for rank, hit in enumerate(retrieval.hits[:3], 1):
            if hit.path in seen_paths:
                continue
            seen_paths.add(hit.path)
            source = (context.repo_root / hit.path).read_text(
                encoding="utf-8-sig", errors="replace"
            )[:80_000]
            packets.append(
                tool_observation_packet(
                    f"tool-read:{case.case_id}:{rank}",
                    source,
                    citations=(hit.citation,),
                    priority=88 if rank == 1 else 45,
                )
            )
        baseline = baseline_builder.build(tuple(packets))
        built = budgeted_builder.build(tuple(packets))
        raw_tokens.append(baseline.estimated_input_tokens)
        budgeted_tokens.append(built.estimated_input_tokens)
        mandatory_ids = {"system-instructions", "user-task", "working-state"}
        mandatory_ok = mandatory_ids.issubset(built.included_packet_ids)
        mandatory_retained += int(mandatory_ok)
        relevant_ok = relevant_packet_id in built.included_packet_ids
        relevant_retained += int(relevant_ok)
        compression_count += len(built.compressions)
        details.append(
            {
                "case_id": case.case_id,
                "raw_tokens": baseline.estimated_input_tokens,
                "budgeted_tokens": built.estimated_input_tokens,
                "relevant_evidence_retained": relevant_ok,
                "mandatory_context_retained": mandatory_ok,
                "compressed_packets": len(built.compressions),
            }
        )
        if position % 10 == 0 or position == len(cases):
            print(f"  context engineering: {position}/{len(cases)}", flush=True)
    raw_mean = statistics.fmean(raw_tokens)
    budgeted_mean = statistics.fmean(budgeted_tokens)
    return {
        "case_count": len(cases),
        "model_context_window": 8_000,
        "reserved_output_tokens": 1_000,
        "evaluation_scope": "selection_and_budgeting_given_relevant_evidence",
        "mean_raw_tokens": raw_mean,
        "mean_budgeted_tokens": budgeted_mean,
        "mean_token_reduction": 1.0 - (budgeted_mean / raw_mean),
        "p95_budgeted_tokens": _percentile(budgeted_tokens, 0.95),
        "relevant_evidence_retention": relevant_retained / len(cases),
        "mandatory_context_retention": mandatory_retained / len(cases),
        "compressed_packets": compression_count,
        "cases": details,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repetitions", type=int, default=1)
    parser.add_argument("--reuse-existing", action="store_true")
    arguments = parser.parse_args()

    dsn = os.environ["REPO_AGENT_POSTGRES_DSN"]
    cases = _load_cases(arguments.dataset)
    state_dir = arguments.output.parent / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    context = ProjectContextResolver(ProjectRegistry(state_dir / "projects.json")).resolve(
        repo=arguments.repo
    )
    if context.commit_sha != EXPECTED_DJANGO_COMMIT or context.is_dirty:
        raise RuntimeError(
            "Django benchmark repository must be clean and pinned to "
            f"{EXPECTED_DJANGO_COMMIT}; got {context.revision}"
        )

    embedding = FeatureHashEmbeddingClient(256)
    search_config = HybridSearchConfig(max_dense_scan_chunks=100_000)
    sqlite_path = state_dir / "django-rag.sqlite3"
    previous_report = None
    if arguments.reuse_existing and arguments.output.exists():
        previous_report = json.loads(arguments.output.read_text(encoding="utf-8"))
    reuse_existing = (
        arguments.reuse_existing
        and sqlite_path.exists()
        and previous_report is not None
        and previous_report.get("repository", {}).get("commit") == context.commit_sha
    )
    if not reuse_existing:
        sqlite_path.unlink(missing_ok=True)
    sqlite_index = SQLiteRAGIndex(
        sqlite_path, embedding, search_config=search_config
    )
    postgres_index = PostgresRAGIndex(
        dsn, embedding, search_config=search_config
    )
    try:
        if reuse_existing:
            print("[1/7] Reusing committed SQLite and PostgreSQL indexes", flush=True)
        else:
            print("[1/7] Resetting PostgreSQL benchmark rows", flush=True)
            with postgres_index._connection.transaction():
                with postgres_index._connection.cursor() as cursor:
                    cursor.execute(
                        "DELETE FROM repository_chunks WHERE project_id = %s",
                        (context.project_id,),
                    )
                    cursor.execute(
                        "DELETE FROM repository_files WHERE project_id = %s",
                        (context.project_id,),
                    )
                    cursor.execute(
                        "DELETE FROM repository_index_state WHERE project_id = %s",
                        (context.project_id,),
                    )

        if reuse_existing:
            print("[2/7] Validating existing SQLite index state", flush=True)
            sqlite_index._validate_state(context)
            print("[3/7] Validating existing PostgreSQL index state", flush=True)
            postgres_index._validate_state(context)
            sqlite_index_seconds = 0.0
            postgres_index_seconds = 0.0
        else:
            print("[2/7] Indexing Django into SQLite exact-scan backend", flush=True)
            sqlite_started = time.perf_counter()
            sqlite_report = sqlite_index.index_repository(context)
            sqlite_index_seconds = time.perf_counter() - sqlite_started
            print(
                f"  SQLite indexed {sqlite_report.written_chunks} chunks in "
                f"{sqlite_index_seconds:.1f}s",
                flush=True,
            )
            print("[3/7] Indexing Django into PostgreSQL/pgvector", flush=True)
            postgres_started = time.perf_counter()
            postgres_report = postgres_index.index_repository(context)
            postgres_index_seconds = time.perf_counter() - postgres_started
        with postgres_index._connection.cursor() as cursor:
            cursor.execute("ANALYZE repository_chunks")
        if not reuse_existing:
            print(
                f"  PostgreSQL indexed {postgres_report.written_chunks} chunks in "
                f"{postgres_index_seconds:.1f}s and refreshed planner statistics",
                flush=True,
            )

        print("[4/7] Measuring retrieval quality", flush=True)
        if reuse_existing:
            quality = dict(previous_report["quality"])
            for mode in ("lexical", "hybrid"):
                print(f" PostgreSQL {mode}", flush=True)
                quality[f"postgres_{mode}"] = _quality(
                    postgres_index, context, cases, mode, 5
                )
            print("[5/7] Reusing unchanged dense latency measurements", flush=True)
            latency = previous_report["latency"]
            ann_recall = previous_report["ann_recall_at_10"]
        else:
            quality = {}
            for mode in ("lexical", "dense", "hybrid"):
                print(f" SQLite {mode}", flush=True)
                quality[f"sqlite_{mode}"] = _quality(
                    sqlite_index, context, cases, mode, 5
                )
                print(f" PostgreSQL {mode}", flush=True)
                quality[f"postgres_{mode}"] = _quality(
                    postgres_index, context, cases, mode, 5
                )

            print("[5/7] Measuring dense retrieval latency", flush=True)
            latency = {}
            for mode in ("dense",):
                print(f" SQLite {mode}", flush=True)
                latency[f"sqlite_{mode}"] = _latency(
                    sqlite_index,
                    context,
                    cases,
                    mode,
                    10,
                    arguments.repetitions,
                )
                print(f" PostgreSQL {mode}", flush=True)
                latency[f"postgres_{mode}"] = _latency(
                    postgres_index,
                    context,
                    cases,
                    mode,
                    10,
                    arguments.repetitions,
                )

            print("[6/7] Comparing HNSW results with exact top-10", flush=True)
            ann_recall = _ann_recall(
                sqlite_index, postgres_index, context, cases, 10
            )
        if reuse_existing:
            print("[6/7] Revalidating the HNSW query plan", flush=True)
        query_plan = _explain_hnsw(
            postgres_index, context, embedding, cases[0].query
        )
        print(
            f"  planner indexes: {query_plan['index_names'] or ['none']}",
            flush=True,
        )
        print("[7/7] Measuring context budget and evidence retention", flush=True)
        context_metrics = _context_metrics(postgres_index, context, cases)
        report = {
            "schema": "repo-agent-django-rag-context-benchmark-v1",
            "repository": {
                "name": "django/django",
                "commit": context.commit_sha,
                "indexed_files": (
                    previous_report["repository"]["indexed_files"]
                    if reuse_existing else sqlite_report.scanned_files
                ),
                "chunks": sqlite_index.count_chunks(context.project_id),
                "embedding": embedding.model_id,
                "dimensions": embedding.dimensions,
            },
            "dataset": {
                "path": str(arguments.dataset),
                "case_count": len(cases),
            },
            "indexing": {
                "sqlite_seconds": (
                    previous_report["indexing"]["sqlite_seconds"]
                    if reuse_existing else sqlite_index_seconds
                ),
                "postgres_seconds": (
                    previous_report["indexing"]["postgres_seconds"]
                    if reuse_existing else postgres_index_seconds
                ),
                "sqlite": (
                    previous_report["indexing"]["sqlite"]
                    if reuse_existing else sqlite_report.model_dump(mode="json")
                ),
                "postgres": (
                    previous_report["indexing"]["postgres"]
                    if reuse_existing else postgres_report.model_dump(mode="json")
                ),
                "reused_existing_indexes": reuse_existing,
            },
            "quality": quality,
            "latency": latency,
            "ann_recall_at_10": ann_recall,
            "query_plan": query_plan,
            "context_engineering": context_metrics,
        }
        arguments.output.parent.mkdir(parents=True, exist_ok=True)
        arguments.output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        if not report["query_plan"]["hnsw_index_used"]:
            raise RuntimeError(
                "PostgreSQL dense query did not use an HNSW index; "
                f"diagnostic report written to {arguments.output}"
            )
        print(json.dumps(report, ensure_ascii=False, indent=2))
    finally:
        sqlite_index.close()
        postgres_index.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
