"""Offline patch evaluation runner."""

from __future__ import annotations

from pathlib import Path
import shutil
import sys
import time
from typing import Callable
from uuid import uuid4

from repo_agent.candidate import (
    CandidateEvaluationConfig,
    CandidateFileChange,
    CandidatePatch,
    CandidatePatchApplier,
    CandidateWorkspace,
    ObjectiveCandidateEvaluator,
    sha256_bytes,
)
from repo_agent.projects import ProjectContextResolver, ProjectRegistry
from repo_agent.tools.process import SecureSubprocessRunner

from .loader import load_patch_cases
from .models import EvalCaseResult, EvalReport, PatchEvalCase, RunMetrics


PatchProvider = Callable[[Path, PatchEvalCase], CandidatePatch]


def _copy_fixture(source: Path, destination: Path) -> None:
    shutil.copytree(
        source,
        destination,
        ignore=shutil.ignore_patterns(
            ".git",
            ".venv",
            "venv",
            "__pycache__",
            ".pytest_cache",
        ),
    )


def _run_pytest(repo_root: Path, targets: tuple[str, ...]) -> bool:
    result = SecureSubprocessRunner().run(
        (sys.executable, "-m", "pytest", *targets),
        cwd=repo_root,
        timeout_seconds=60,
        output_limit=20_000,
    )
    return result.exit_code == 0


def default_patch_provider(repo_root: Path, case: PatchEvalCase) -> CandidatePatch:
    """Small deterministic provider for the committed offline fixtures."""

    if case.expected_changed_paths != ("src/calculator.py",):
        raise ValueError(f"no default patch for case {case.case_id}")
    path = repo_root / "src" / "calculator.py"
    original = path.read_text(encoding="utf-8")
    fixed = original.replace("return left - right", "return left + right", 1)
    if fixed == original:
        raise ValueError(f"default patch did not change fixture for case {case.case_id}")
    return CandidatePatch(
        patch_id=f"eval-{case.case_id}",
        summary="Fix calculator addition implementation",
        changes=(
            CandidateFileChange(
                path="src/calculator.py",
                expected_sha256=sha256_bytes(path.read_bytes()),
                replacement_content=fixed,
                reason="The add function must add, not subtract.",
            ),
        ),
    )


def evaluate_patch_cases(
    dataset: str | Path,
    *,
    fixtures_root: str | Path = "evals/fixtures",
    state_dir: str | Path | None = None,
    patch_provider: PatchProvider = default_patch_provider,
) -> EvalReport:
    """Run patch cases in fresh temporary repository copies."""

    started = time.perf_counter()
    dataset_path = Path(dataset)
    fixture_root = Path(fixtures_root)
    cases = load_patch_cases(dataset_path, fixtures_root=fixture_root)
    if state_dir is None:
        local_temp_root = Path.cwd() / ".test-tmp"
        local_temp_root.mkdir(parents=True, exist_ok=True)
        resolved_state = local_temp_root / f"repo-agent-eval-patch-{uuid4().hex}"
        resolved_state.mkdir()
    else:
        resolved_state = Path(state_dir)
    resolved_state.mkdir(parents=True, exist_ok=True)
    results: list[EvalCaseResult] = []
    for case in cases:
        case_root = resolved_state / "patch-cases" / case.case_id
        if case_root.exists():
            shutil.rmtree(case_root)
        repo_copy = case_root / "repo"
        _copy_fixture(fixture_root / case.repo_fixture, repo_copy)
        baseline_failed = not _run_pytest(repo_copy, case.target_tests)
        if not baseline_failed:
            results.append(
                EvalCaseResult(
                    case_id=case.case_id,
                    passed=False,
                    metrics={"baseline_failed": False},
                    details={"error": "target tests did not fail before patch"},
                )
            )
            continue
        context = ProjectContextResolver(
            ProjectRegistry(resolved_state / "projects.json")
        ).resolve(repo=repo_copy)
        patch = patch_provider(repo_copy, case)
        with CandidateWorkspace(
            context,
            resolved_state / "candidate-workspaces",
            f"eval-{case.case_id}",
        ) as workspace:
            CandidatePatchApplier(workspace).apply(patch)
            evaluation = ObjectiveCandidateEvaluator(
                workspace,
                CandidateEvaluationConfig(
                    expected_changed_files=case.expected_changed_paths,
                    target_tests=case.target_tests,
                    regression_targets=case.regression_tests,
                    allow_code_execution=True,
                ),
            ).evaluate_candidate()
        changed = set(evaluation.changed_files)
        forbidden = changed.intersection(case.forbidden_changed_paths)
        expected = set(case.expected_changed_paths)
        passed = (
            baseline_failed
            and evaluation.passed
            and expected.issubset(changed)
            and not forbidden
        )
        results.append(
            EvalCaseResult(
                case_id=case.case_id,
                passed=passed,
                metrics={
                    "baseline_failed": baseline_failed,
                    "evaluation_passed": evaluation.passed,
                    "changed_file_count": len(evaluation.changed_files),
                },
                details={
                    "changed_files": evaluation.changed_files,
                    "forbidden_changed_paths": tuple(sorted(forbidden)),
                    "summary": evaluation.summary,
                },
            )
        )
    return EvalReport(
        suite="patch",
        dataset=str(dataset_path),
        passed=all(item.passed for item in results),
        case_count=len(results),
        metrics=RunMetrics(
            duration_ms=int((time.perf_counter() - started) * 1000),
            patch_attempts=len(results),
            llm_requests=0,
            tool_calls=None,
        ),
        cases=tuple(results),
    )
