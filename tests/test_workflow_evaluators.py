from __future__ import annotations

from pathlib import Path
import sys
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from repo_agent.workflow import (
    EvidenceBasedDiagnoseEvaluator,
    EvaluationRequest,
    ExecutionPlan,
    PlanStep,
    StepExecution,
    StepToolObservation,
)


class EvidenceBasedEvaluatorTests(unittest.TestCase):
    def test_line_citations_are_prioritized_over_directory_entries(self) -> None:
        plan = ExecutionPlan(
            rationale="先看目录，再读取入口",
            steps=(
                PlanStep(
                    id="inspect",
                    goal="解释入口",
                    expected_evidence=("文件与行号",),
                    allowed_tools=("list_files", "read_file_range"),
                ),
            ),
        )
        directory_entries = [
            {"path": f"package/module_{index}.py"}
            for index in range(30)
        ]
        execution = StepExecution(
            step_id="inspect",
            status="completed",
            summary="入口位于 app/main.py",
            react_status="completed",
            stop_reason="模型返回最终答案",
            iterations=3,
            tool_calls=2,
            observations=(
                StepToolObservation(
                    iteration=1,
                    tool_name="list_files",
                    arguments={},
                    decision_summary="查看目录",
                    result={
                        "status": "success",
                        "data": directory_entries,
                        "error": None,
                        "metadata": {},
                    },
                ),
                StepToolObservation(
                    iteration=2,
                    tool_name="read_file_range",
                    arguments={"path": "app/main.py"},
                    decision_summary="读取入口",
                    result={
                        "status": "success",
                        "data": {
                            "path": "app/main.py",
                            "start_line": 1,
                            "end_line": 80,
                            "content": "app = FastAPI()",
                        },
                        "error": None,
                        "metadata": {},
                    },
                ),
            ),
        )

        result = EvidenceBasedDiagnoseEvaluator().evaluate(
            EvaluationRequest(
                run_id="run-1",
                project_id="project-1",
                repo_revision="revision-1",
                user_goal="解释入口",
                plan=plan,
                step_results=(execution,),
                mode="diagnose",
            )
        )

        self.assertTrue(result.passed)
        self.assertEqual(result.evidence[0], "app/main.py:1-80")
        self.assertEqual(len(result.evidence), 20)


if __name__ == "__main__":
    unittest.main()
