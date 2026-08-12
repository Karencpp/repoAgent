"""Offline retrieval evaluation runner."""

from __future__ import annotations

from pathlib import Path
import time
from typing import Literal
from uuid import uuid4

from repo_agent.projects import ProjectContextResolver, ProjectRegistry
from repo_agent.rag import FeatureHashEmbeddingClient, SQLiteRAGIndex

from .loader import load_retrieval_cases
from .models import EvalCaseResult, EvalReport, RetrievalEvalCase, RunMetrics


def _fixture_context(fixtures_root: Path, repo_fixture: str, state_dir: Path):
    registry = ProjectRegistry(state_dir / "projects.json")
    return ProjectContextResolver(registry).resolve(
        repo=fixtures_root / repo_fixture
    )


def _citation_accuracy(fixture_root: Path, case: RetrievalEvalCase, paths: set[str]) -> float:
    if not case.relevant_line_ranges:
        return 1.0
    valid = 0
    for expected in case.relevant_line_ranges:
        source = fixture_root / case.repo_fixture / expected.path
        if expected.path not in paths or not source.is_file():
            continue
        line_count = len(source.read_text(encoding="utf-8").splitlines())
        if expected.end_line <= line_count:
            valid += 1
    return valid / len(case.relevant_line_ranges)


def evaluate_retrieval_cases(
    dataset: str | Path,
    *,
    fixtures_root: str | Path = "evals/fixtures",
    state_dir: str | Path | None = None,
    mode: Literal["lexical", "dense", "hybrid"] = "hybrid",
) -> EvalReport:
    """Run retrieval cases against the local SQLite RAG backend."""

    started = time.perf_counter()
    dataset_path = Path(dataset)
    fixture_root = Path(fixtures_root)
    cases = load_retrieval_cases(dataset_path, fixtures_root=fixture_root)
    if state_dir is None:
        local_temp_root = Path.cwd() / ".test-tmp"
        local_temp_root.mkdir(parents=True, exist_ok=True)
        resolved_state = local_temp_root / f"repo-agent-eval-retrieval-{uuid4().hex}"
        resolved_state.mkdir()
    else:
        resolved_state = Path(state_dir)
    resolved_state.mkdir(parents=True, exist_ok=True)
    results: list[EvalCaseResult] = []
    embedding = FeatureHashEmbeddingClient(256)
    index = SQLiteRAGIndex(resolved_state / "rag.sqlite3", embedding)
    try:
        contexts = {
            fixture: _fixture_context(fixture_root, fixture, resolved_state)
            for fixture in sorted({case.repo_fixture for case in cases})
        }
        for context in contexts.values():
            index.index_repository(context)
        for case in cases:
            context = contexts[case.repo_fixture]
            retrieval = index.search(context, case.query, top_k=case.top_k, mode=mode)
            retrieved_paths = tuple(hit.path for hit in retrieval.hits)
            retrieved_set = set(retrieved_paths)
            relevant = set(case.relevant_paths)
            found = relevant.intersection(retrieved_set)
            first_rank = next(
                (
                    rank
                    for rank, hit in enumerate(retrieval.hits, 1)
                    if hit.path in relevant
                    or (hit.symbol is not None and hit.symbol in case.relevant_symbols)
                ),
                None,
            )
            recall = len(found) / len(relevant)
            hit = first_rank is not None
            citation_accuracy = _citation_accuracy(fixture_root, case, retrieved_set)
            results.append(
                EvalCaseResult(
                    case_id=case.case_id,
                    passed=hit,
                    metrics={
                        "recall_at_k": recall,
                        "mrr": 1.0 / first_rank if first_rank else 0.0,
                        "hit_at_k": hit,
                        "citation_accuracy": citation_accuracy,
                    },
                    details={
                        "query": case.query,
                        "retrieved_paths": retrieved_paths,
                        "relevant_paths": case.relevant_paths,
                        "mode": mode,
                    },
                )
            )
    finally:
        index.close()
    duration_ms = int((time.perf_counter() - started) * 1000)
    return EvalReport(
        suite="retrieval",
        dataset=str(dataset_path),
        passed=all(item.passed for item in results),
        case_count=len(results),
        metrics=RunMetrics(
            duration_ms=duration_ms,
            rag_queries=len(results),
            llm_requests=None,
            tool_calls=None,
            patch_attempts=None,
        ),
        cases=tuple(results),
    )
