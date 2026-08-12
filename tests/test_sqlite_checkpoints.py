from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import shutil
import sys
import unittest
from uuid import uuid4


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
TEST_TEMP_ROOT = PROJECT_ROOT / ".checkpoint-test-tmp"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from repo_agent.projects import ProjectContextResolver, ProjectRegistry
from repo_agent.workflow import (
    CheckpointNotFoundError,
    CheckpointRevisionMismatchError,
    CheckpointStoreClosedError,
    CheckpointThreadExistsError,
    EvaluationResult,
    ExecutionPlan,
    InvalidCheckpointThreadError,
    PlanStep,
    ReflectionResult,
    SQLiteWorkflowRuntime,
    ScriptedEvaluator,
    ScriptedPlanner,
    ScriptedReflector,
    ScriptedStepExecutor,
    StepExecution,
    build_checkpoint_thread_id,
)


def make_plan(step_id: str = "locate") -> ExecutionPlan:
    """创建单步骤测试计划。"""

    return ExecutionPlan(
        rationale="先定位代码",
        steps=(
            PlanStep(
                id=step_id,
                goal="定位 BillingService",
                expected_evidence=("文件路径",),
                allowed_tools=("search_code",),
            ),
        ),
    )


def completed(step_id: str = "locate") -> StepExecution:
    """创建成功步骤结果。"""

    return StepExecution(
        step_id=step_id,
        status="completed",
        summary="已经找到定义",
        react_status="completed",
        stop_reason="模型返回最终答案",
        iterations=2,
        tool_calls=1,
    )


def failed(step_id: str = "locate") -> StepExecution:
    """创建失败步骤结果。"""

    return StepExecution(
        step_id=step_id,
        status="failed",
        summary="第一次搜索没有证据",
        react_status="budget_exhausted",
        stop_reason="工具预算耗尽",
        iterations=2,
        tool_calls=1,
    )


def passed() -> EvaluationResult:
    """创建通过评估。"""

    return EvaluationResult(
        passed=True,
        summary="证据满足目标",
        evidence=("src/billing.py:1",),
    )


def rejected() -> EvaluationResult:
    """创建失败评估。"""

    return EvaluationResult(
        passed=False,
        summary="需要缩小搜索范围",
        issues=("证据不足",),
    )


class SQLiteCheckpointTests(unittest.TestCase):
    def setUp(self) -> None:
        TEST_TEMP_ROOT.mkdir(parents=True, exist_ok=True)
        self.root = TEST_TEMP_ROOT / f"case-{uuid4().hex}"
        self.repo_one = self.root / "repo-one"
        self.repo_two = self.root / "repo-two"
        for repo in (self.repo_one, self.repo_two):
            (repo / "src").mkdir(parents=True)
            (repo / "src" / "billing.py").write_text(
                "class BillingService:\n    pass\n",
                encoding="utf-8",
            )
        self.registry = ProjectRegistry(self.root / "state" / "projects.json")
        resolver = ProjectContextResolver(self.registry)
        self.context_one = resolver.resolve(repo=self.repo_one)
        self.context_two = resolver.resolve(repo=self.repo_two)
        self.database_path = self.root / "runtime" / "checkpoints.sqlite"

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def runtime(
        self,
        *,
        planner: ScriptedPlanner,
        executor: ScriptedStepExecutor,
        evaluator: ScriptedEvaluator,
        reflector: ScriptedReflector | None = None,
        interrupt_before: tuple[str, ...] = (),
    ) -> SQLiteWorkflowRuntime:
        """创建连接同一测试数据库的 Runtime。"""

        return SQLiteWorkflowRuntime(
            self.database_path,
            planner,
            executor,
            evaluator,
            reflector or ScriptedReflector(()),
            interrupt_before=interrupt_before,
        )

    def test_completed_run_persists_latest_state_and_history(self) -> None:
        with self.runtime(
            planner=ScriptedPlanner([make_plan()]),
            executor=ScriptedStepExecutor([completed()]),
            evaluator=ScriptedEvaluator([passed()]),
        ) as runtime:
            result = runtime.start(
                self.context_one,
                "定位服务",
                thread_id="task-1",
                run_id="run-1",
            )
            latest = runtime.latest(self.context_one, thread_id="task-1")
            history = runtime.history(self.context_one, thread_id="task-1")

        self.assertEqual(result.status, "completed")
        self.assertEqual(result.thread_id, "task-1")
        self.assertEqual(latest.run_id, "run-1")
        self.assertEqual(latest.status, "completed")
        self.assertTrue(latest.is_terminal)
        self.assertEqual(latest.next_nodes, ())
        self.assertGreaterEqual(len(history), 5)
        self.assertEqual(history[0].checkpoint_id, latest.checkpoint_id)
        self.assertTrue(self.database_path.exists())

    def test_interrupted_run_resumes_in_new_runtime_without_reexecuting_step(self) -> None:
        first_executor = ScriptedStepExecutor([completed()])
        with self.runtime(
            planner=ScriptedPlanner([make_plan()]),
            executor=first_executor,
            evaluator=ScriptedEvaluator(()),
            interrupt_before=("evaluate",),
        ) as first_runtime:
            interrupted = first_runtime.start(
                self.context_one,
                "定位服务",
                thread_id="resume-1",
                run_id="stable-run",
            )
            latest = first_runtime.latest(self.context_one, thread_id="resume-1")

        second_planner = ScriptedPlanner(())
        second_executor = ScriptedStepExecutor(())
        with self.runtime(
            planner=second_planner,
            executor=second_executor,
            evaluator=ScriptedEvaluator([passed()]),
        ) as second_runtime:
            resumed = second_runtime.resume(
                self.context_one,
                thread_id="resume-1",
            )

        self.assertEqual(interrupted.status, "interrupted")
        self.assertEqual(latest.next_nodes, ("evaluate",))
        self.assertEqual(resumed.status, "completed")
        self.assertEqual(resumed.run_id, "stable-run")
        self.assertEqual(len(first_executor.requests), 1)
        self.assertEqual(second_planner.planning_requests, [])
        self.assertEqual(second_executor.requests, [])

    def test_start_rejects_existing_thread_instead_of_merging_state(self) -> None:
        with self.runtime(
            planner=ScriptedPlanner([make_plan()]),
            executor=ScriptedStepExecutor([completed()]),
            evaluator=ScriptedEvaluator([passed()]),
        ) as runtime:
            runtime.start(self.context_one, "定位服务", thread_id="same")

            with self.assertRaises(CheckpointThreadExistsError):
                runtime.start(self.context_one, "另一个目标", thread_id="same")

    def test_same_logical_thread_is_isolated_between_projects(self) -> None:
        planner = ScriptedPlanner([make_plan("one"), make_plan("two")])
        executor = ScriptedStepExecutor([completed("one"), completed("two")])
        evaluator = ScriptedEvaluator([passed(), passed()])
        with self.runtime(
            planner=planner,
            executor=executor,
            evaluator=evaluator,
        ) as runtime:
            one = runtime.start(self.context_one, "项目一", thread_id="shared")

            with self.assertRaises(CheckpointNotFoundError):
                runtime.latest(self.context_two, thread_id="shared")

            two = runtime.start(self.context_two, "项目二", thread_id="shared")
            latest_one = runtime.latest(self.context_one, thread_id="shared")
            latest_two = runtime.latest(self.context_two, thread_id="shared")

        self.assertNotEqual(
            build_checkpoint_thread_id(self.context_one, "shared"),
            build_checkpoint_thread_id(self.context_two, "shared"),
        )
        self.assertEqual(one.project_id, self.context_one.project_id)
        self.assertEqual(two.project_id, self.context_two.project_id)
        self.assertEqual(latest_one.project_id, self.context_one.project_id)
        self.assertEqual(latest_two.project_id, self.context_two.project_id)

    def test_resume_rejects_changed_repository_revision(self) -> None:
        with self.runtime(
            planner=ScriptedPlanner([make_plan()]),
            executor=ScriptedStepExecutor([completed()]),
            evaluator=ScriptedEvaluator(()),
            interrupt_before=("evaluate",),
        ) as runtime:
            runtime.start(self.context_one, "定位服务", thread_id="stale")

        fresh_context = replace(
            self.context_one,
            revision=f"{self.context_one.revision}:changed",
        )
        self.assertEqual(fresh_context.project_id, self.context_one.project_id)
        self.assertNotEqual(fresh_context.revision, self.context_one.revision)

        with self.runtime(
            planner=ScriptedPlanner(()),
            executor=ScriptedStepExecutor(()),
            evaluator=ScriptedEvaluator([passed()]),
        ) as runtime:
            historical = runtime.latest(fresh_context, thread_id="stale")
            with self.assertRaises(CheckpointRevisionMismatchError):
                runtime.resume(fresh_context, thread_id="stale")

        self.assertEqual(historical.repo_revision, self.context_one.revision)

    def test_resuming_completed_thread_is_idempotent(self) -> None:
        with self.runtime(
            planner=ScriptedPlanner([make_plan()]),
            executor=ScriptedStepExecutor([completed()]),
            evaluator=ScriptedEvaluator([passed()]),
        ) as runtime:
            original = runtime.start(
                self.context_one,
                "定位服务",
                thread_id="done",
            )

        planner = ScriptedPlanner(())
        executor = ScriptedStepExecutor(())
        evaluator = ScriptedEvaluator(())
        with self.runtime(
            planner=planner,
            executor=executor,
            evaluator=evaluator,
        ) as runtime:
            resumed = runtime.resume(self.context_one, thread_id="done")

        self.assertEqual(resumed.status, "completed")
        self.assertEqual(resumed.run_id, original.run_id)
        self.assertEqual(planner.planning_requests, [])
        self.assertEqual(executor.requests, [])
        self.assertEqual(evaluator.requests, [])

    def test_history_limit_and_delete_are_scoped_to_project_thread(self) -> None:
        with self.runtime(
            planner=ScriptedPlanner([make_plan("one"), make_plan("two")]),
            executor=ScriptedStepExecutor([completed("one"), completed("two")]),
            evaluator=ScriptedEvaluator([passed(), passed()]),
        ) as runtime:
            runtime.start(self.context_one, "项目一", thread_id="shared")
            runtime.start(self.context_two, "项目二", thread_id="shared")
            limited = runtime.history(
                self.context_one,
                thread_id="shared",
                limit=2,
            )
            runtime.delete(self.context_one, thread_id="shared")

            with self.assertRaises(CheckpointNotFoundError):
                runtime.latest(self.context_one, thread_id="shared")
            remaining = runtime.latest(self.context_two, thread_id="shared")

        self.assertEqual(len(limited), 2)
        self.assertEqual(remaining.project_id, self.context_two.project_id)

    def test_invalid_thread_ids_are_rejected(self) -> None:
        for thread_id in ("", "../escape", "with space", "a" * 101):
            with self.subTest(thread_id=thread_id):
                with self.assertRaises(InvalidCheckpointThreadError):
                    build_checkpoint_thread_id(self.context_one, thread_id)

    def test_closed_runtime_rejects_access(self) -> None:
        runtime = self.runtime(
            planner=ScriptedPlanner(()),
            executor=ScriptedStepExecutor(()),
            evaluator=ScriptedEvaluator(()),
        )

        with self.assertRaises(CheckpointStoreClosedError):
            _ = runtime.workflow
        with runtime:
            self.assertIsNotNone(runtime.workflow)
        with self.assertRaises(CheckpointStoreClosedError):
            _ = runtime.checkpointer

    def test_retry_attempts_use_distinct_execution_keys(self) -> None:
        executor = ScriptedStepExecutor([failed(), completed()])
        with self.runtime(
            planner=ScriptedPlanner([make_plan()]),
            executor=executor,
            evaluator=ScriptedEvaluator([rejected(), passed()]),
            reflector=ScriptedReflector(
                [
                    ReflectionResult(
                        failure_cause="查询范围过宽",
                        corrective_action="缩小范围后重试",
                        should_replan=False,
                    )
                ]
            ),
        ) as runtime:
            result = runtime.start(
                self.context_one,
                "定位服务",
                thread_id="retry",
                run_id="stable-run",
            )

        first_key = executor.requests[0].execution_key
        second_key = executor.requests[1].execution_key
        self.assertEqual(result.status, "completed")
        self.assertNotEqual(first_key, second_key)
        self.assertEqual(result.step_results[0].execution_key, first_key)
        self.assertEqual(result.step_results[1].execution_key, second_key)


if __name__ == "__main__":
    unittest.main()
