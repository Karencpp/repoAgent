from __future__ import annotations

from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
import shutil
import sys
import unittest
from unittest.mock import patch
from uuid import uuid4


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
TEST_TEMP_ROOT = PROJECT_ROOT / ".test-tmp" / "maintenance-cli-e2e"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from repo_agent.candidate import CandidateEvaluationReport, ValidationCheck
from repo_agent.cli import main
from repo_agent.maintenance_workflow import MaintenanceRunResult


class FakeMaintenanceService:
    started = []
    resumed = []

    def __init__(self, config):
        self.config = config

    def start_workflow(self, objective, **kwargs):
        self.__class__.started.append((objective, kwargs))
        return MaintenanceRunResult(
            run_id="run-cli",
            thread_id=kwargs.get("thread_id") or "fix-thread",
            project_id="project-cli",
            repo_root=str(kwargs["repo"]),
            repo_revision="manifest:test",
            objective=objective,
            status="waiting_approval",
            stop_reason="waiting for approval",
            analysis=None,
            selected_targets=None,
            patch=None,
            patch_history=(),
            patch_attempt=1,
            evaluation=CandidateEvaluationReport(
                passed=True,
                changed_files=("src/main.py",),
                checks=(
                    ValidationCheck(
                        name="target_tests",
                        status="passed",
                        summary="pytest passed",
                    ),
                ),
                unified_diff="--- a/src/main.py\n+++ b/src/main.py\n",
                summary="passed",
            ),
            evaluation_history=(),
            reflection=None,
            reflection_history=(),
            proposal_id="proposal-cli",
            approval_status="pending",
            promotion_result=None,
            final_report="waiting",
            trace=(),
        )

    def resume_workflow(self, **kwargs):
        self.__class__.resumed.append(kwargs)
        return MaintenanceRunResult(
            run_id="run-cli",
            thread_id=kwargs["thread_id"],
            project_id="project-cli",
            repo_root=str(kwargs["repo"]),
            repo_revision="manifest:test",
            objective="fix objective",
            status="completed" if kwargs["approved"] else "failed",
            stop_reason="approved patch promoted" if kwargs["approved"] else "proposal rejected",
            analysis=None,
            selected_targets=None,
            patch=None,
            patch_history=(),
            patch_attempt=1,
            evaluation=None,
            evaluation_history=(),
            reflection=None,
            reflection_history=(),
            proposal_id="proposal-cli",
            approval_status="approved" if kwargs["approved"] else "rejected",
            promotion_result=None,
            final_report="completed",
            trace=(),
        )


class MaintenanceCLIE2ETests(unittest.TestCase):
    def setUp(self) -> None:
        FakeMaintenanceService.started = []
        FakeMaintenanceService.resumed = []
        self.root = TEST_TEMP_ROOT / uuid4().hex
        self.repo = self.root / "repo"
        (self.repo / "src").mkdir(parents=True)
        (self.repo / "src" / "main.py").write_text("def main():\n    return 'ok'\n", encoding="utf-8")

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def test_fix_cli_uses_maintenance_workflow_and_prints_resume_command(self) -> None:
        output = StringIO()
        with patch("repo_agent.cli.RepoAgentMaintenanceService", FakeMaintenanceService):
            with redirect_stdout(output):
                status = main(
                    [
                        "--state-dir",
                        str(self.root / "state"),
                        "fix",
                        "--repo",
                        str(self.repo),
                        "fix objective",
                        "--allow-code-execution",
                    ]
                )

        self.assertEqual(status, 0)
        self.assertEqual(FakeMaintenanceService.started[0][0], "fix objective")
        self.assertTrue(FakeMaintenanceService.started[0][1]["allow_code_execution"])
        self.assertIn("repo-agent resume-fix", output.getvalue())

    def test_resume_fix_cli_uses_checkpointed_decision(self) -> None:
        output = StringIO()
        with patch("repo_agent.cli.RepoAgentMaintenanceService", FakeMaintenanceService):
            with redirect_stdout(output):
                status = main(
                    [
                        "--state-dir",
                        str(self.root / "state"),
                        "resume-fix",
                        "--repo",
                        str(self.repo),
                        "--thread-id",
                        "fix-thread",
                        "--approve",
                    ]
                )

        self.assertEqual(status, 0)
        self.assertEqual(FakeMaintenanceService.resumed[0]["thread_id"], "fix-thread")
        self.assertTrue(FakeMaintenanceService.resumed[0]["approved"])
        self.assertIn("completed", output.getvalue())


if __name__ == "__main__":
    unittest.main()
