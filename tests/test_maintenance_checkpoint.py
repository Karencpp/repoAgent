from __future__ import annotations

from pathlib import Path
import shutil
import sys
import unittest
from uuid import uuid4


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
TEST_TEMP_ROOT = PROJECT_ROOT / ".test-tmp" / "maintenance-checkpoint"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from repo_agent.candidate import (
    CandidateEvaluationReport,
    CandidateFileChange,
    CandidatePatch,
    CandidatePromotionResult,
    PatchApplicationResult,
    PatchTargetSelection,
    ValidationCheck,
    sha256_bytes,
)
from repo_agent.maintenance_workflow import PatchEvaluationArtifact, RepositoryAnalysis
from repo_agent.maintenance_workflow.runtime import SQLiteMaintenanceWorkflowRuntime
from repo_agent.projects import ProjectContextResolver, ProjectRegistry


class Analyzer:
    def analyze(self, context, objective, *, thread_id=None):
        return RepositoryAnalysis(
            run_id="analysis-run",
            thread_id=thread_id or "thread",
            report="analysis",
        )


class Selector:
    def select(self, context, objective, analysis):
        return PatchTargetSelection(
            rationale="select calculator",
            paths=("src/calculator.py",),
            target_tests=("tests/test_calculator.py",),
            regression_targets=("tests",),
        )


class Proposer:
    def __init__(self, patch):
        self.patch = patch

    def propose(self, request):
        return self.patch


class Evaluator:
    def evaluate(self, context, patch, selection, *, attempt):
        return PatchEvaluationArtifact(
            application=PatchApplicationResult(
                patch_id=patch.patch_id,
                summary=patch.summary,
                changed_files=("src/calculator.py",),
                changes=(),
                unified_diff="diff",
            ),
            evaluation=CandidateEvaluationReport(
                passed=True,
                changed_files=("src/calculator.py",),
                checks=(
                    ValidationCheck(
                        name="target_tests",
                        status="passed",
                        summary="pytest passed",
                    ),
                ),
                unified_diff="diff",
                summary="passed",
            ),
        )


class Reflector:
    def reflect(self, *args, **kwargs):  # pragma: no cover
        raise AssertionError("reflect should not run")


class Store:
    def save(self, result):
        return "proposal-checkpoint", "proposal-checkpoint.json"


class Promoter:
    def __init__(self):
        self.calls = []

    def promote(self, context, proposal_id, *, approved):
        self.calls.append((proposal_id, approved))
        return CandidatePromotionResult(
            proposal_id=proposal_id,
            project_id=context.project_id,
            source_revision=context.revision,
            changed_files=("src/calculator.py",),
            promoted_at="2026-08-07T00:00:00+00:00",
        )


class MaintenanceCheckpointTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = TEST_TEMP_ROOT / uuid4().hex
        self.repo = self.root / "repo"
        (self.repo / "src").mkdir(parents=True)
        (self.repo / "tests").mkdir()
        calculator = self.repo / "src" / "calculator.py"
        calculator.write_text("def add(left, right):\n    return left - right\n", encoding="utf-8")
        (self.repo / "tests" / "test_calculator.py").write_text(
            "def test_placeholder():\n    assert True\n",
            encoding="utf-8",
        )
        self.context = ProjectContextResolver(
            ProjectRegistry(self.root / "state" / "projects.json")
        ).resolve(repo=self.repo)
        self.patch = CandidatePatch(
            patch_id="fix-add",
            summary="fix add",
            changes=(
                CandidateFileChange(
                    path="src/calculator.py",
                    expected_sha256=sha256_bytes(calculator.read_bytes()),
                    replacement_content="def add(left, right):\n    return left + right\n",
                    reason="fix",
                ),
            ),
        )

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def runtime(self, promoter: Promoter):
        return SQLiteMaintenanceWorkflowRuntime(
            self.root / "state" / "maintenance.sqlite3",
            context=self.context,
            analyzer=Analyzer(),
            selector=Selector(),
            proposer=Proposer(self.patch),
            evaluator=Evaluator(),
            reflector=Reflector(),
            proposal_store=Store(),
            promoter=promoter,
        )

    def test_waiting_approval_can_resume_after_runtime_reopen(self) -> None:
        first_promoter = Promoter()
        with self.runtime(first_promoter) as runtime:
            result = runtime.start("fix add", thread_id="fix-thread")

        self.assertEqual(result.status, "waiting_approval")
        self.assertEqual(result.proposal_id, "proposal-checkpoint")
        self.assertFalse(first_promoter.calls)

        second_promoter = Promoter()
        with self.runtime(second_promoter) as runtime:
            resumed = runtime.resume(thread_id="fix-thread", approved=True)

        self.assertEqual(resumed.status, "completed")
        self.assertEqual(second_promoter.calls, [("proposal-checkpoint", True)])


if __name__ == "__main__":
    unittest.main()
