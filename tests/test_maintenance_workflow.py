from __future__ import annotations

from pathlib import Path
import shutil
import sys
import unittest
from collections import deque
from uuid import uuid4


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
TEST_TEMP_ROOT = PROJECT_ROOT / ".test-tmp" / "maintenance-workflow"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from repo_agent.candidate import (
    CandidateEvaluationReport,
    CandidateFileChange,
    CandidatePatch,
    PatchApplicationResult,
    PatchTargetSelection,
    ValidationCheck,
    sha256_bytes,
)
from repo_agent.maintenance_workflow import (
    MaintenanceWorkflowConfig,
    PatchEvaluationArtifact,
    PatchReflection,
    RepoAgentMaintenanceWorkflow,
    RepositoryAnalysis,
)
from repo_agent.maintenance_workflow.adapters import ObjectivePatchReflector
from repo_agent.projects import ProjectContextResolver, ProjectRegistry


class Analyzer:
    def analyze(self, context, objective, *, thread_id=None):
        return RepositoryAnalysis(
            run_id="analysis-run",
            thread_id=thread_id or "analysis-thread",
            report="calculator analysis",
            relevant_files=("src/calculator.py",),
        )


class Selector:
    def select(self, context, objective, analysis):
        return PatchTargetSelection(
            rationale="calculator only",
            paths=("src/calculator.py",),
            target_tests=("tests/test_calculator.py::test_add",),
            regression_targets=("tests",),
        )


class Proposer:
    def __init__(self, patches):
        self.patches = deque(patches)
        self.requests = []

    def propose(self, request):
        self.requests.append(request)
        return self.patches.popleft()


class Evaluator:
    def __init__(self, artifacts):
        self.artifacts = deque(artifacts)

    def evaluate(self, context, patch, selection, *, attempt):
        return self.artifacts.popleft()


class Reflector:
    def __init__(self):
        self.requests = []

    def reflect(self, context, objective, patch, artifact, *, attempt):
        self.requests.append((artifact, attempt))
        return PatchReflection(
            failure_kind="target_tests",
            corrective_action=artifact.evaluation.checks[0].stdout,
            next_action="repair",
        )


class ProposalStore:
    def __init__(self):
        self.saved = []

    def save(self, result):
        self.saved.append(result)
        return "proposal-test", "proposal-test.json"


class Promoter:
    def __init__(self):
        self.calls = []

    def promote(self, context, proposal_id, *, approved):
        self.calls.append((proposal_id, approved))
        from repo_agent.candidate import CandidatePromotionResult

        return CandidatePromotionResult(
            proposal_id=proposal_id,
            project_id=context.project_id,
            source_revision=context.revision,
            changed_files=("src/calculator.py",),
            promoted_at="2026-08-07T00:00:00+00:00",
        )


def artifact(passed: bool, stdout: str = "") -> PatchEvaluationArtifact:
    status = "passed" if passed else "failed"
    return PatchEvaluationArtifact(
        application=PatchApplicationResult(
            patch_id="patch",
            summary="patch",
            changed_files=("src/calculator.py",),
            changes=(),
            unified_diff="diff",
        ),
        evaluation=CandidateEvaluationReport(
            passed=passed,
            changed_files=("src/calculator.py",),
            checks=(
                ValidationCheck(
                    name="target_tests",
                    status=status,
                    summary="pytest",
                    stdout=stdout,
                ),
            ),
            unified_diff="diff",
            summary="passed" if passed else "failed",
        ),
    )


class MaintenanceWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = TEST_TEMP_ROOT / uuid4().hex
        self.repo = self.root / "repo"
        (self.repo / "src").mkdir(parents=True)
        (self.repo / "tests").mkdir()
        (self.repo / "src" / "__init__.py").write_text("", encoding="utf-8")
        (self.repo / "src" / "calculator.py").write_text(
            "def add(left, right):\n    return left - right\n",
            encoding="utf-8",
        )
        (self.repo / "tests" / "test_calculator.py").write_text(
            "from src.calculator import add\n\n"
            "def test_add():\n    assert add(2, 3) == 5\n",
            encoding="utf-8",
        )
        self.context = ProjectContextResolver(
            ProjectRegistry(self.root / "state" / "projects.json")
        ).resolve(repo=self.repo)

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def make_patch(self, patch_id: str, content: str) -> CandidatePatch:
        return CandidatePatch(
            patch_id=patch_id,
            summary=patch_id,
            changes=(
                CandidateFileChange(
                    path="src/calculator.py",
                    expected_sha256=sha256_bytes(
                        (self.repo / "src" / "calculator.py").read_bytes()
                    ),
                    replacement_content=content,
                    reason="fix",
                ),
            ),
        )

    def test_failed_patch_reflects_and_second_patch_waits_for_approval(self) -> None:
        bad_patch = self.make_patch("bad", "def add(left, right):\n    return 0\n")
        good_patch = self.make_patch("good", "def add(left, right):\n    return left + right\n")
        proposer = Proposer([bad_patch, good_patch])
        reflector = Reflector()
        store = ProposalStore()
        workflow = RepoAgentMaintenanceWorkflow(
            Analyzer(),
            Selector(),
            proposer,
            Evaluator([artifact(False, "assert 0 == 5"), artifact(True)]),
            reflector,
            store,
            Promoter(),
            context=self.context,
            config=MaintenanceWorkflowConfig(max_patch_attempts=3),
        )

        result = workflow.run("fix add")

        self.assertEqual(result.status, "waiting_approval")
        self.assertEqual(result.patch_attempt, 2)
        self.assertEqual(len(result.reflection_history), 1)
        self.assertIn("assert 0 == 5", reflector.requests[0][0].evaluation.checks[0].stdout)
        self.assertEqual(store.saved[0].patch.patch_id, "good")

    def test_duplicate_patch_fingerprint_stops_loop(self) -> None:
        repeated = self.make_patch("same", "def add(left, right):\n    return 0\n")
        workflow = RepoAgentMaintenanceWorkflow(
            Analyzer(),
            Selector(),
            Proposer([repeated, repeated]),
            Evaluator([artifact(False, "failed")]),
            Reflector(),
            ProposalStore(),
            Promoter(),
            context=self.context,
            config=MaintenanceWorkflowConfig(max_patch_attempts=3),
        )

        result = workflow.run("fix add")

        self.assertEqual(result.status, "failed")
        self.assertIn("duplicate patch fingerprint", result.stop_reason)

    def test_objective_reflector_includes_pytest_output(self) -> None:
        reflection = ObjectivePatchReflector().reflect(
            self.context,
            "fix add",
            self.make_patch("bad", "def add(left, right):\n    return 0\n"),
            artifact(False, "E assert 0 == 5"),
            attempt=1,
        )

        self.assertEqual(reflection.next_action, "repair")
        self.assertIn("E assert 0 == 5", reflection.corrective_action)


if __name__ == "__main__":
    unittest.main()
