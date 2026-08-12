"""Deterministic offline explain evaluation runner."""

from __future__ import annotations

from pathlib import Path
import time

from .loader import load_explain_cases
from .models import EvalCaseResult, EvalReport, RunMetrics


def evaluate_explain_cases(
    dataset: str | Path,
    *,
    fixtures_root: str | Path = "evals/fixtures",
) -> EvalReport:
    """Evaluate explain datasets without a live LLM judge."""

    started = time.perf_counter()
    dataset_path = Path(dataset)
    fixture_root = Path(fixtures_root)
    cases = load_explain_cases(dataset_path, fixtures_root=fixture_root)
    results: list[EvalCaseResult] = []
    for case in cases:
        repo_root = fixture_root / case.repo_fixture
        missing_paths = [
            path for path in case.required_paths if not (repo_root / path).is_file()
        ]
        corpus = "\n".join(
            (repo_root / path).read_text(encoding="utf-8")
            for path in case.required_paths
            if (repo_root / path).is_file()
        )
        missing_claims = [claim for claim in case.required_claims if claim not in corpus]
        forbidden_hits = [
            claim for claim in case.forbidden_claims if claim and claim in corpus
        ]
        passed = not missing_paths and not missing_claims and not forbidden_hits
        results.append(
            EvalCaseResult(
                case_id=case.case_id,
                passed=passed,
                metrics={
                    "required_path_hits": len(case.required_paths) - len(missing_paths),
                    "required_claim_hits": len(case.required_claims) - len(missing_claims),
                    "forbidden_claim_hits": len(forbidden_hits),
                },
                details={
                    "missing_paths": tuple(missing_paths),
                    "missing_claims": tuple(missing_claims),
                    "forbidden_hits": tuple(forbidden_hits),
                },
            )
        )
    return EvalReport(
        suite="explain",
        dataset=str(dataset_path),
        passed=all(item.passed for item in results),
        case_count=len(results),
        metrics=RunMetrics(
            duration_ms=int((time.perf_counter() - started) * 1000),
            llm_requests=0,
            tool_calls=0,
            rag_queries=0,
            memory_queries=0,
        ),
        cases=tuple(results),
    )
