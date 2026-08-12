from __future__ import annotations

from pathlib import Path
import sys
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from repo_agent.evals import evaluate_patch_cases, evaluate_retrieval_cases
from repo_agent.evals.explain_runner import evaluate_explain_cases


class EvalRunnerTests(unittest.TestCase):
    def test_retrieval_runner_reports_case_metrics(self) -> None:
        report = evaluate_retrieval_cases(
            PROJECT_ROOT / "evals" / "retrieval" / "python-small.jsonl",
            fixtures_root=PROJECT_ROOT / "evals" / "fixtures",
            state_dir=PROJECT_ROOT / ".test-tmp" / "eval-runner-retrieval",
        )

        self.assertTrue(report.passed)
        self.assertEqual(report.case_count, 5)
        self.assertIn("recall_at_k", report.cases[0].metrics)

    def test_explain_runner_is_deterministic(self) -> None:
        report = evaluate_explain_cases(
            PROJECT_ROOT / "evals" / "explain" / "python-small.jsonl",
            fixtures_root=PROJECT_ROOT / "evals" / "fixtures",
        )

        self.assertTrue(report.passed)
        self.assertEqual(report.metrics.llm_requests, 0)

    def test_patch_runner_verifies_baseline_failure_and_repair(self) -> None:
        report = evaluate_patch_cases(
            PROJECT_ROOT / "evals" / "patch" / "python-small.jsonl",
            fixtures_root=PROJECT_ROOT / "evals" / "fixtures",
            state_dir=PROJECT_ROOT / ".test-tmp" / "eval-runner-patch",
        )

        self.assertTrue(report.passed)
        self.assertTrue(all(case.metrics["baseline_failed"] for case in report.cases))


if __name__ == "__main__":
    unittest.main()
