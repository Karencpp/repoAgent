from __future__ import annotations

from collections import deque
from pathlib import Path
import shutil
import sys
import unittest
from uuid import uuid4


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
TEST_TEMP_ROOT = PROJECT_ROOT / ".candidate-test-tmp"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from pydantic import ValidationError

from repo_agent.candidate import (
    CandidateEvaluationConfig,
    CandidateFileChange,
    CandidatePatch,
    CandidatePatchApplier,
    CandidatePatchConflictError,
    CandidatePatchError,
    CandidatePatchPermissionError,
    CandidateWorkspace,
    CandidateWorkspaceConfig,
    CandidateWorkspaceError,
    CandidateWorkspaceLimitError,
    ObjectiveCandidateEvaluator,
    sha256_bytes,
)
from repo_agent.projects import ProjectContextResolver, ProjectRegistry
from repo_agent.tools import ProcessResult
from repo_agent.workflow import (
    EvaluationRequest,
    ExecutionPlan,
    PlanStep,
    RepoAgentWorkflow,
    ScriptedPlanner,
    ScriptedReflector,
    ScriptedStepExecutor,
    StepExecution,
)


BUGGY_SOURCE = """def add(left: int, right: int) -> int:
    \"\"\"返回两个整数之和。\"\"\"
    return left - right
"""

FIXED_SOURCE = """def add(left: int, right: int) -> int:
    \"\"\"返回两个整数之和。\"\"\"
    return left + right
"""


class SequenceProcessRunner:
    """按顺序返回进程结果并记录调用。"""

    def __init__(self, responses: list[ProcessResult]) -> None:
        self.responses = deque(responses)
        self.calls: list[dict[str, object]] = []

    def run(
        self,
        command,
        *,
        cwd: Path,
        timeout_seconds: float,
        output_limit: int,
    ) -> ProcessResult:
        self.calls.append(
            {
                "command": tuple(command),
                "cwd": cwd,
                "timeout_seconds": timeout_seconds,
                "output_limit": output_limit,
            }
        )
        if not self.responses:
            raise AssertionError("进程测试替身没有剩余响应")
        return self.responses.popleft()


class FailIfCalledRunner:
    """任何进程执行都让测试失败。"""

    def run(self, command, *, cwd, timeout_seconds, output_limit):
        raise AssertionError("当前分支不应执行项目代码")


def process_result(exit_code: int, output: str) -> ProcessResult:
    """创建 pytest 进程观察。"""

    return ProcessResult(
        command=(sys.executable, "-m", "pytest"),
        exit_code=exit_code,
        stdout=output,
        stderr="",
        duration_ms=10,
        timed_out=False,
        output_truncated=False,
    )


def completed_step() -> StepExecution:
    """创建 Graph 使用的成功步骤结果。"""

    return StepExecution(
        step_id="patch",
        status="completed",
        summary="候选补丁已经生成",
        react_status="completed",
        stop_reason="模型返回最终答案",
        iterations=1,
        tool_calls=0,
    )


class CandidateEvaluationTests(unittest.TestCase):
    def setUp(self) -> None:
        TEST_TEMP_ROOT.mkdir(parents=True, exist_ok=True)
        self.root = TEST_TEMP_ROOT / f"case-{uuid4().hex}"
        self.repo = self.root / "target-repo"
        (self.repo / "src").mkdir(parents=True)
        (self.repo / "tests").mkdir()
        (self.repo / ".venv").mkdir()
        (self.repo / "src" / "__init__.py").write_text("", encoding="utf-8")
        (self.repo / "src" / "calculator.py").write_text(
            BUGGY_SOURCE,
            encoding="utf-8",
        )
        (self.repo / "tests" / "test_calculator.py").write_text(
            """from src.calculator import add


def test_add():
    assert add(2, 3) == 5


def test_negative_numbers():
    assert add(-2, -3) == -5
""",
            encoding="utf-8",
        )
        (self.repo / "README.md").write_text("# Calculator\n", encoding="utf-8")
        (self.repo / ".venv" / "secret.txt").write_text(
            "不应复制",
            encoding="utf-8",
        )
        registry = ProjectRegistry(self.root / "state" / "projects.json")
        self.context = ProjectContextResolver(registry).resolve(repo=self.repo)
        self.workspace_base = self.root / "candidate-workspaces"

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def make_patch(
        self,
        *,
        content: str = FIXED_SOURCE,
        expected_sha256: str | None = None,
    ) -> CandidatePatch:
        """创建 calculator.py 的候选补丁。"""

        return CandidatePatch(
            patch_id="fix-add",
            summary="修复加法误用减法",
            changes=(
                CandidateFileChange(
                    path="src/calculator.py",
                    expected_sha256=expected_sha256
                    or sha256_bytes(
                        (self.repo / "src" / "calculator.py").read_bytes()
                    ),
                    replacement_content=content,
                    reason="实现与函数契约不一致",
                ),
            ),
        )

    def evaluation_config(
        self,
        *,
        allow_code_execution: bool = True,
    ) -> CandidateEvaluationConfig:
        """创建标准候选评估配置。"""

        return CandidateEvaluationConfig(
            expected_changed_files=("src/calculator.py",),
            target_tests=("tests/test_calculator.py::test_add",),
            regression_targets=("tests",),
            allow_code_execution=allow_code_execution,
        )

    def test_workspace_copies_repository_without_cache_and_cleans_up(self) -> None:
        run_root = self.workspace_base / "run-copy"
        with CandidateWorkspace(
            self.context,
            self.workspace_base,
            "run-copy",
        ) as workspace:
            self.assertTrue((workspace.worktree_root / "src" / "calculator.py").exists())
            self.assertFalse((workspace.worktree_root / ".venv").exists())
            self.assertEqual(workspace.context.project_id, self.context.project_id)
            self.assertNotEqual(workspace.context.repo_root, self.context.repo_root)

        self.assertFalse(run_root.exists())
        self.assertTrue((self.repo / "src" / "calculator.py").exists())

    def test_patch_changes_only_workspace_and_generates_diff(self) -> None:
        source_before = (self.repo / "src" / "calculator.py").read_bytes()
        with CandidateWorkspace(
            self.context,
            self.workspace_base,
            "run-patch",
        ) as workspace:
            result = CandidatePatchApplier(workspace).apply(self.make_patch())

            self.assertEqual(
                (workspace.worktree_root / "src" / "calculator.py").read_text(
                    encoding="utf-8"
                ),
                FIXED_SOURCE,
            )
            self.assertEqual(workspace.changed_files(), ("src/calculator.py",))
            self.assertIn("-    return left - right", result.unified_diff)
            self.assertIn("+    return left + right", result.unified_diff)

        self.assertEqual(
            (self.repo / "src" / "calculator.py").read_bytes(),
            source_before,
        )

    def test_patch_can_create_and_delete_text_files_inside_workspace(self) -> None:
        readme_hash = sha256_bytes((self.repo / "README.md").read_bytes())
        patch = CandidatePatch(
            patch_id="create-delete",
            summary="新增回归测试并删除旧说明",
            changes=(
                CandidateFileChange(
                    path="tests/test_regression.py",
                    operation="create",
                    replacement_content="def test_regression():\n    assert True\n",
                    reason="增加回归覆盖",
                ),
                CandidateFileChange(
                    path="README.md",
                    operation="delete",
                    expected_sha256=readme_hash,
                    reason="删除废弃说明",
                ),
            ),
        )

        with CandidateWorkspace(
            self.context,
            self.workspace_base,
            "run-create-delete",
        ) as workspace:
            result = CandidatePatchApplier(workspace).apply(patch)

            self.assertTrue(
                (workspace.worktree_root / "tests" / "test_regression.py").is_file()
            )
            self.assertFalse((workspace.worktree_root / "README.md").exists())
            self.assertEqual(
                workspace.changed_files(),
                ("README.md", "tests/test_regression.py"),
            )
            self.assertEqual(
                [change.operation for change in result.changes],
                ["create", "delete"],
            )

        self.assertTrue((self.repo / "README.md").is_file())
        self.assertFalse((self.repo / "tests" / "test_regression.py").exists())

    def test_stale_hash_rejects_patch_without_writing(self) -> None:
        with CandidateWorkspace(
            self.context,
            self.workspace_base,
            "run-conflict",
        ) as workspace:
            before = (workspace.worktree_root / "src" / "calculator.py").read_bytes()
            with self.assertRaises(CandidatePatchConflictError):
                CandidatePatchApplier(workspace).apply(
                    self.make_patch(expected_sha256="0" * 64)
                )
            after = (workspace.worktree_root / "src" / "calculator.py").read_bytes()

        self.assertEqual(before, after)

    def test_all_changes_are_validated_before_any_file_is_written(self) -> None:
        readme_before = (self.repo / "README.md").read_text(encoding="utf-8")
        patch = CandidatePatch(
            patch_id="two-files",
            summary="同时修改两个文件",
            changes=(
                self.make_patch().changes[0],
                CandidateFileChange(
                    path="README.md",
                    expected_sha256="0" * 64,
                    replacement_content="# Changed\n",
                    reason="更新说明",
                ),
            ),
        )
        with CandidateWorkspace(
            self.context,
            self.workspace_base,
            "run-atomic",
        ) as workspace:
            with self.assertRaises(CandidatePatchConflictError):
                CandidatePatchApplier(workspace).apply(patch)

            calculator = (workspace.worktree_root / "src" / "calculator.py").read_text(
                encoding="utf-8"
            )
            readme = (workspace.worktree_root / "README.md").read_text(
                encoding="utf-8"
            )

        self.assertEqual(calculator, BUGGY_SOURCE)
        self.assertEqual(readme, readme_before)

    def test_patch_rejects_unsafe_path_and_file_type(self) -> None:
        with self.assertRaises(ValidationError):
            CandidateFileChange(
                path="../outside.py",
                expected_sha256="0" * 64,
                replacement_content="",
                reason="越界",
            )
        binary = self.repo / "payload.bin"
        binary.write_bytes(b"data")
        fresh_context = ProjectContextResolver(
            ProjectRegistry(self.root / "state-two" / "projects.json")
        ).resolve(repo=self.repo)
        with CandidateWorkspace(
            fresh_context,
            self.workspace_base,
            "run-binary",
        ) as workspace:
            patch = CandidatePatch(
                patch_id="binary",
                summary="尝试修改二进制",
                changes=(
                    CandidateFileChange(
                        path="payload.bin",
                        expected_sha256=sha256_bytes(b"data"),
                        replacement_content="changed",
                        reason="不允许",
                    ),
                ),
            )
            with self.assertRaises(CandidatePatchPermissionError):
                CandidatePatchApplier(workspace).apply(patch)

    def test_objective_evaluator_runs_real_compile_target_and_regression_tests(self) -> None:
        with CandidateWorkspace(
            self.context,
            self.workspace_base,
            "run-real-tests",
        ) as workspace:
            CandidatePatchApplier(workspace).apply(self.make_patch())
            evaluator = ObjectiveCandidateEvaluator(
                workspace,
                self.evaluation_config(),
            )

            report = evaluator.evaluate_candidate()

        self.assertTrue(report.passed)
        self.assertEqual(
            [check.status for check in report.checks],
            ["passed", "passed", "passed", "passed"],
        )
        self.assertIn("src/calculator.py", report.changed_files)

    def test_compile_failure_skips_all_project_execution(self) -> None:
        broken = "def broken(:\n    pass\n"
        with CandidateWorkspace(
            self.context,
            self.workspace_base,
            "run-compile-fail",
        ) as workspace:
            CandidatePatchApplier(workspace).apply(self.make_patch(content=broken))
            evaluator = ObjectiveCandidateEvaluator(
                workspace,
                self.evaluation_config(),
                process_runner=FailIfCalledRunner(),
            )

            report = evaluator.evaluate_candidate()

        self.assertFalse(report.passed)
        self.assertEqual(
            [check.status for check in report.checks],
            ["passed", "failed", "skipped", "skipped"],
        )

    def test_unexpected_change_fails_scope_and_prevents_execution(self) -> None:
        with CandidateWorkspace(
            self.context,
            self.workspace_base,
            "run-scope",
        ) as workspace:
            CandidatePatchApplier(workspace).apply(self.make_patch())
            (workspace.worktree_root / "README.md").write_text(
                "# Unexpected\n",
                encoding="utf-8",
            )
            evaluator = ObjectiveCandidateEvaluator(
                workspace,
                self.evaluation_config(),
                process_runner=FailIfCalledRunner(),
            )

            report = evaluator.evaluate_candidate()

        self.assertFalse(report.passed)
        self.assertEqual(report.checks[0].status, "failed")
        self.assertIn("未授权变更", report.checks[0].summary)
        self.assertEqual(report.checks[2].status, "skipped")

    def test_missing_code_execution_authorization_is_an_evaluation_error(self) -> None:
        with CandidateWorkspace(
            self.context,
            self.workspace_base,
            "run-no-auth",
        ) as workspace:
            CandidatePatchApplier(workspace).apply(self.make_patch())
            evaluator = ObjectiveCandidateEvaluator(
                workspace,
                self.evaluation_config(allow_code_execution=False),
                process_runner=FailIfCalledRunner(),
            )

            report = evaluator.evaluate_candidate()

        self.assertFalse(report.passed)
        self.assertEqual(report.checks[2].status, "error")
        self.assertIn("显式授权", report.checks[2].summary)
        self.assertEqual(report.checks[3].status, "skipped")

    def test_target_failure_skips_regression_and_preserves_observation(self) -> None:
        runner = SequenceProcessRunner([process_result(1, "1 failed")])
        with CandidateWorkspace(
            self.context,
            self.workspace_base,
            "run-target-fail",
        ) as workspace:
            CandidatePatchApplier(workspace).apply(self.make_patch())
            evaluator = ObjectiveCandidateEvaluator(
                workspace,
                self.evaluation_config(),
                process_runner=runner,
            )

            report = evaluator.evaluate_candidate()

        self.assertFalse(report.passed)
        self.assertEqual(report.checks[2].status, "failed")
        self.assertEqual(report.checks[2].stdout, "1 failed")
        self.assertEqual(report.checks[3].status, "skipped")
        self.assertEqual(len(runner.calls), 1)

    def test_regression_failure_is_distinct_from_target_failure(self) -> None:
        runner = SequenceProcessRunner(
            [process_result(0, "1 passed"), process_result(1, "1 failed")]
        )
        with CandidateWorkspace(
            self.context,
            self.workspace_base,
            "run-regression-fail",
        ) as workspace:
            CandidatePatchApplier(workspace).apply(self.make_patch())
            evaluator = ObjectiveCandidateEvaluator(
                workspace,
                self.evaluation_config(),
                process_runner=runner,
            )

            report = evaluator.evaluate_candidate()

        self.assertFalse(report.passed)
        self.assertEqual(report.checks[2].status, "passed")
        self.assertEqual(report.checks[3].status, "failed")
        self.assertEqual(len(runner.calls), 2)

    def test_objective_evaluator_implements_workflow_evaluator_port(self) -> None:
        runner = SequenceProcessRunner(
            [process_result(0, "1 passed"), process_result(0, "2 passed")]
        )
        plan = ExecutionPlan(
            rationale="生成候选补丁后验证",
            steps=(
                PlanStep(
                    id="patch",
                    goal="生成候选补丁",
                    expected_evidence=("unified diff",),
                    allowed_tools=("read_file_range",),
                ),
            ),
        )
        with CandidateWorkspace(
            self.context,
            self.workspace_base,
            "run-workflow",
        ) as workspace:
            CandidatePatchApplier(workspace).apply(self.make_patch())
            evaluator = ObjectiveCandidateEvaluator(
                workspace,
                self.evaluation_config(),
                process_runner=runner,
            )
            workflow = RepoAgentWorkflow(
                ScriptedPlanner([plan]),
                ScriptedStepExecutor([completed_step()]),
                evaluator,
                ScriptedReflector(()),
            )

            result = workflow.run(self.context, "修复 add")

        self.assertEqual(result.status, "completed")
        self.assertTrue(result.evaluation.passed)
        self.assertEqual(len(evaluator.reports), 1)
        self.assertIn("check:regression_tests:passed", result.evaluation.evidence)

    def test_workspace_limits_and_location_are_enforced(self) -> None:
        with self.assertRaises(CandidateWorkspaceError):
            CandidateWorkspace(
                self.context,
                self.workspace_base,
                "..",
            )
        with self.assertRaises(CandidateWorkspaceError):
            with CandidateWorkspace(
                self.context,
                self.repo / "nested-workspaces",
                "run-inside",
            ):
                pass
        with self.assertRaises(CandidateWorkspaceLimitError):
            with CandidateWorkspace(
                self.context,
                self.workspace_base,
                "run-limit",
                config=CandidateWorkspaceConfig(max_files=1),
            ):
                pass

    def test_workspace_growth_limit_becomes_structured_evaluation_error(self) -> None:
        with CandidateWorkspace(
            self.context,
            self.workspace_base,
            "run-growth-limit",
            config=CandidateWorkspaceConfig(max_single_file_bytes=10_000),
        ) as workspace:
            CandidatePatchApplier(workspace).apply(self.make_patch())
            (workspace.worktree_root / "generated.txt").write_text(
                "x" * 20_000,
                encoding="utf-8",
            )
            evaluator = ObjectiveCandidateEvaluator(
                workspace,
                self.evaluation_config(),
                process_runner=FailIfCalledRunner(),
            )

            report = evaluator.evaluate_candidate()

        self.assertFalse(report.passed)
        self.assertEqual(report.checks[0].status, "error")
        self.assertEqual(report.checks[2].status, "skipped")

    def test_workflow_evaluator_rejects_project_mismatch(self) -> None:
        plan = ExecutionPlan(
            rationale="验证项目身份",
            steps=(
                PlanStep(
                    id="patch",
                    goal="生成补丁",
                    expected_evidence=("diff",),
                    allowed_tools=("read_file_range",),
                ),
            ),
        )
        with CandidateWorkspace(
            self.context,
            self.workspace_base,
            "run-project-mismatch",
        ) as workspace:
            evaluator = ObjectiveCandidateEvaluator(
                workspace,
                self.evaluation_config(),
                process_runner=FailIfCalledRunner(),
            )
            result = evaluator.evaluate(
                EvaluationRequest(
                    run_id="run-other",
                    project_id="other-project",
                    repo_revision=self.context.revision,
                    user_goal="修复 add",
                    plan=plan,
                    step_results=(completed_step(),),
                    mode="fix",
                )
            )

        self.assertFalse(result.passed)
        self.assertIn("项目身份不一致", result.summary)


if __name__ == "__main__":
    unittest.main()
