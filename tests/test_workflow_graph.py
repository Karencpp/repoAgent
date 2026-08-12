from __future__ import annotations

from pathlib import Path
import shutil
import sys
import unittest
from uuid import uuid4


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
TEST_TEMP_ROOT = PROJECT_ROOT / ".workflow-test-tmp"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from pydantic import ValidationError

from repo_agent.projects import ProjectContextResolver, ProjectRegistry
from repo_agent.react import ReActExecutor, ScriptedDecisionClient, StructuredDecisionModel
from repo_agent.tools import LocalRepositoryTools, build_repository_tool_registry
from repo_agent.workflow import (
    EvaluationResult,
    ExecutionPlan,
    PlanStep,
    ReActStepExecutor,
    ReflectionResult,
    RepoAgentWorkflow,
    ScriptedEvaluator,
    ScriptedPlanner,
    ScriptedReflector,
    ScriptedStepExecutor,
    StepExecution,
    WorkflowConfig,
)


def make_step(step_id: str, goal: str | None = None) -> PlanStep:
    """创建测试使用的待执行步骤。"""

    return PlanStep(
        id=step_id,
        goal=goal or f"完成 {step_id}",
        expected_evidence=("路径和行号",),
        allowed_tools=("search_code",),
    )


def make_plan(*step_ids: str, rationale: str = "先定位再验证") -> ExecutionPlan:
    """创建包含指定步骤的有限计划。"""

    return ExecutionPlan(
        rationale=rationale,
        steps=tuple(make_step(step_id) for step_id in step_ids),
    )


def completed(step_id: str, summary: str | None = None) -> StepExecution:
    """创建成功的步骤执行结果。"""

    return StepExecution(
        step_id=step_id,
        status="completed",
        summary=summary or f"{step_id} 已完成",
        react_status="completed",
        stop_reason="模型返回最终答案",
        iterations=1,
        tool_calls=0,
    )


def failed(step_id: str, summary: str = "证据不足") -> StepExecution:
    """创建失败的步骤执行结果。"""

    return StepExecution(
        step_id=step_id,
        status="failed",
        summary=summary,
        react_status="budget_exhausted",
        stop_reason="工具预算已耗尽",
        iterations=2,
        tool_calls=1,
    )


def passed_evaluation() -> EvaluationResult:
    """创建通过的客观评估。"""

    return EvaluationResult(
        passed=True,
        summary="目标和证据均已满足",
        evidence=("src/billing.py:1",),
    )


def rejected_evaluation(summary: str = "缺少验证证据") -> EvaluationResult:
    """创建未通过的客观评估。"""

    return EvaluationResult(
        passed=False,
        summary=summary,
        issues=(summary,),
    )


class WorkflowGraphTests(unittest.TestCase):
    def setUp(self) -> None:
        TEST_TEMP_ROOT.mkdir(parents=True, exist_ok=True)
        self.root = TEST_TEMP_ROOT / f"case-{uuid4().hex}"
        self.repo = self.root / "target-repo"
        (self.repo / "src").mkdir(parents=True)
        (self.repo / "src" / "billing.py").write_text(
            "class BillingService:\n    pass\n",
            encoding="utf-8",
        )
        registry = ProjectRegistry(self.root / "state" / "projects.json")
        self.context = ProjectContextResolver(registry).resolve(repo=self.repo)

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def build_workflow(
        self,
        *,
        planner: ScriptedPlanner,
        executor: ScriptedStepExecutor,
        evaluator: ScriptedEvaluator,
        reflector: ScriptedReflector | None = None,
        config: WorkflowConfig | None = None,
    ) -> RepoAgentWorkflow:
        """组装使用确定性节点替身的工作流。"""

        return RepoAgentWorkflow(
            planner,
            executor,
            evaluator,
            reflector or ScriptedReflector(()),
            config=config,
        )

    def test_happy_path_executes_plan_in_order_and_reports(self) -> None:
        planner = ScriptedPlanner([make_plan("locate", "explain")])
        executor = ScriptedStepExecutor(
            [completed("locate"), completed("explain")]
        )
        evaluator = ScriptedEvaluator([passed_evaluation()])
        workflow = self.build_workflow(
            planner=planner,
            executor=executor,
            evaluator=evaluator,
        )

        result = workflow.run(self.context, "解释 BillingService")

        self.assertEqual(result.status, "completed")
        self.assertEqual([item.step_id for item in result.step_results], ["locate", "explain"])
        self.assertTrue(result.evaluation.passed)
        self.assertIn("任务通过评估", result.final_report)
        self.assertIn('"status":"completed"', result.model_dump_json())
        self.assertEqual(
            [event.node for event in result.trace],
            ["plan", "execute_step", "execute_step", "evaluate", "report"],
        )
        self.assertEqual(executor.requests[1].previous_results[0].step_id, "locate")

    def test_planner_receives_explicit_project_identity_and_revision(self) -> None:
        planner = ScriptedPlanner([make_plan("locate")])
        workflow = self.build_workflow(
            planner=planner,
            executor=ScriptedStepExecutor([completed("locate")]),
            evaluator=ScriptedEvaluator([passed_evaluation()]),
        )

        result = workflow.run(self.context, "定位服务", mode="diagnose", run_id="run-1")

        request = planner.planning_requests[0]
        self.assertEqual(result.run_id, "run-1")
        self.assertEqual(request.project_id, self.context.project_id)
        self.assertEqual(request.repo_root, str(self.context.repo_root))
        self.assertEqual(request.repo_revision, self.context.revision)

    def test_plan_rejects_duplicate_ids_and_more_than_six_steps(self) -> None:
        with self.assertRaises(ValidationError):
            ExecutionPlan(
                rationale="重复步骤",
                steps=(make_step("same"), make_step("same")),
            )
        with self.assertRaises(ValidationError):
            ExecutionPlan(
                rationale="步骤过多",
                steps=tuple(make_step(f"step-{index}") for index in range(7)),
            )

    def test_planner_failure_is_converted_to_failed_report(self) -> None:
        planner = ScriptedPlanner([RuntimeError("模型不可用")])
        executor = ScriptedStepExecutor(())
        evaluator = ScriptedEvaluator(())
        workflow = self.build_workflow(
            planner=planner,
            executor=executor,
            evaluator=evaluator,
        )

        result = workflow.run(self.context, "定位服务")

        self.assertEqual(result.status, "failed")
        self.assertIn("规划失败", result.stop_reason)
        self.assertEqual(executor.requests, [])
        self.assertEqual(evaluator.requests, [])
        self.assertEqual([event.node for event in result.trace], ["plan", "report"])

    def test_failed_step_can_be_reflected_and_retried_once(self) -> None:
        planner = ScriptedPlanner([make_plan("locate")])
        executor = ScriptedStepExecutor(
            [failed("locate"), completed("locate", "缩小范围后找到定义")]
        )
        evaluator = ScriptedEvaluator(
            [rejected_evaluation(), passed_evaluation()]
        )
        reflector = ScriptedReflector(
            [
                ReflectionResult(
                    failure_cause="搜索范围太宽",
                    corrective_action="限制为 Python 文件后重试",
                    should_replan=False,
                )
            ]
        )
        workflow = self.build_workflow(
            planner=planner,
            executor=executor,
            evaluator=evaluator,
            reflector=reflector,
        )

        result = workflow.run(self.context, "定位服务")

        self.assertEqual(result.status, "completed")
        self.assertEqual(result.reflection_count, 1)
        self.assertEqual(result.replan_count, 0)
        self.assertEqual(len(result.evaluation_history), 2)
        self.assertEqual(len(result.reflection_history), 1)
        self.assertEqual(result.plan.steps[0].attempts, 2)
        self.assertEqual(len(result.step_results), 2)
        self.assertEqual(
            [event.node for event in result.trace],
            [
                "plan",
                "execute_step",
                "evaluate",
                "reflect",
                "execute_step",
                "evaluate",
                "report",
            ],
        )

    def test_reflection_can_trigger_replan_and_preserve_completed_prefix(self) -> None:
        planner = ScriptedPlanner(
            [make_plan("locate")],
            replans=[make_plan("verify", rationale="增加独立验证")],
        )
        executor = ScriptedStepExecutor(
            [completed("locate"), completed("verify")]
        )
        evaluator = ScriptedEvaluator(
            [rejected_evaluation("缺少独立验证"), passed_evaluation()]
        )
        reflector = ScriptedReflector(
            [
                ReflectionResult(
                    failure_cause="原计划不完整",
                    corrective_action="增加验证步骤",
                    should_replan=True,
                )
            ]
        )
        workflow = self.build_workflow(
            planner=planner,
            executor=executor,
            evaluator=evaluator,
            reflector=reflector,
        )

        result = workflow.run(self.context, "定位并验证服务")

        self.assertEqual(result.status, "completed")
        self.assertEqual(result.replan_count, 1)
        self.assertEqual([step.id for step in result.plan.steps], ["locate", "verify"])
        self.assertEqual(len(result.plan_history), 2)
        self.assertEqual([step.id for step in result.plan_history[0].steps], ["locate"])
        self.assertEqual(len(planner.replanning_requests), 1)
        self.assertEqual(
            planner.replanning_requests[0].reflection.failure_cause,
            "原计划不完整",
        )

    def test_reflection_budget_zero_stops_after_first_rejection(self) -> None:
        reflector = ScriptedReflector(())
        workflow = self.build_workflow(
            planner=ScriptedPlanner([make_plan("locate")]),
            executor=ScriptedStepExecutor([completed("locate")]),
            evaluator=ScriptedEvaluator([rejected_evaluation()]),
            reflector=reflector,
            config=WorkflowConfig(max_reflections=0),
        )

        result = workflow.run(self.context, "定位服务")

        self.assertEqual(result.status, "failed")
        self.assertEqual(result.reflection_count, 0)
        self.assertEqual(reflector.requests, [])
        self.assertIn("未通过评估", result.stop_reason)

    def test_replan_budget_zero_prevents_replanner_call(self) -> None:
        planner = ScriptedPlanner([make_plan("locate")])
        workflow = self.build_workflow(
            planner=planner,
            executor=ScriptedStepExecutor([completed("locate")]),
            evaluator=ScriptedEvaluator([rejected_evaluation()]),
            reflector=ScriptedReflector(
                [
                    ReflectionResult(
                        failure_cause="计划错误",
                        corrective_action="重新规划",
                        should_replan=True,
                    )
                ]
            ),
            config=WorkflowConfig(max_replans=0),
        )

        result = workflow.run(self.context, "定位服务")

        self.assertEqual(result.status, "failed")
        self.assertEqual(result.reflection_count, 1)
        self.assertEqual(result.replan_count, 0)
        self.assertEqual(planner.replanning_requests, [])

    def test_second_rejection_stops_after_single_reflection(self) -> None:
        workflow = self.build_workflow(
            planner=ScriptedPlanner([make_plan("locate")]),
            executor=ScriptedStepExecutor([failed("locate"), failed("locate")]),
            evaluator=ScriptedEvaluator(
                [rejected_evaluation("第一次失败"), rejected_evaluation("再次失败")]
            ),
            reflector=ScriptedReflector(
                [
                    ReflectionResult(
                        failure_cause="参数不准确",
                        corrective_action="修正参数后重试",
                        should_replan=False,
                    )
                ]
            ),
        )

        result = workflow.run(self.context, "定位服务")

        self.assertEqual(result.status, "failed")
        self.assertEqual(result.reflection_count, 1)
        self.assertIn("再次失败", result.stop_reason)

    def test_mismatched_step_result_is_treated_as_executor_error(self) -> None:
        workflow = self.build_workflow(
            planner=ScriptedPlanner([make_plan("locate")]),
            executor=ScriptedStepExecutor([completed("other")]),
            evaluator=ScriptedEvaluator([rejected_evaluation()]),
            config=WorkflowConfig(max_reflections=0),
        )

        result = workflow.run(self.context, "定位服务")

        self.assertEqual(result.status, "failed")
        self.assertEqual(result.step_results[0].react_status, "executor_error")
        self.assertIn("步骤结果 id 不匹配", result.step_results[0].summary)

    def test_evaluator_exception_is_converted_to_failed_report(self) -> None:
        workflow = self.build_workflow(
            planner=ScriptedPlanner([make_plan("locate")]),
            executor=ScriptedStepExecutor([completed("locate")]),
            evaluator=ScriptedEvaluator([RuntimeError("测试环境不可用")]),
        )

        result = workflow.run(self.context, "定位服务")

        self.assertEqual(result.status, "failed")
        self.assertIn("评估失败", result.stop_reason)
        self.assertEqual([event.node for event in result.trace][-2:], ["evaluate", "report"])

    def test_reflector_exception_is_converted_to_failed_report(self) -> None:
        workflow = self.build_workflow(
            planner=ScriptedPlanner([make_plan("locate")]),
            executor=ScriptedStepExecutor([completed("locate")]),
            evaluator=ScriptedEvaluator([rejected_evaluation()]),
            reflector=ScriptedReflector([RuntimeError("反思模型不可用")]),
        )

        result = workflow.run(self.context, "定位服务")

        self.assertEqual(result.status, "failed")
        self.assertEqual(result.reflection_count, 1)
        self.assertIn("反思失败", result.stop_reason)

    def test_replanner_exception_is_converted_to_failed_report(self) -> None:
        planner = ScriptedPlanner(
            [make_plan("locate")],
            replans=[RuntimeError("重规划模型不可用")],
        )
        workflow = self.build_workflow(
            planner=planner,
            executor=ScriptedStepExecutor([completed("locate")]),
            evaluator=ScriptedEvaluator([rejected_evaluation()]),
            reflector=ScriptedReflector(
                [
                    ReflectionResult(
                        failure_cause="原计划错误",
                        corrective_action="生成替代计划",
                        should_replan=True,
                    )
                ]
            ),
        )

        result = workflow.run(self.context, "定位服务")

        self.assertEqual(result.status, "failed")
        self.assertEqual(result.replan_count, 1)
        self.assertIn("重规划失败", result.stop_reason)

    def test_real_react_executor_is_reused_as_graph_step(self) -> None:
        tools = LocalRepositoryTools(self.context)
        tool_registry = build_repository_tool_registry(tools)
        decision_client = ScriptedDecisionClient(
            [
                {
                    "type": "tool_call",
                    "tool_name": "search_code",
                    "arguments": {"query": "BillingService"},
                    "decision_summary": "先定位定义",
                },
                {
                    "type": "final_answer",
                    "answer": "定义位于 src/billing.py",
                    "decision_summary": "搜索证据已经足够",
                },
            ]
        )
        react_executor = ReActExecutor(
            StructuredDecisionModel(decision_client),
            tool_registry,
        )
        plan = ExecutionPlan(
            rationale="先搜索定义",
            steps=(make_step("locate", "定位 BillingService"),),
        )
        workflow = RepoAgentWorkflow(
            ScriptedPlanner([plan]),
            ReActStepExecutor(react_executor),
            ScriptedEvaluator([passed_evaluation()]),
            ScriptedReflector(()),
        )

        result = workflow.run(self.context, "定位 BillingService")

        self.assertEqual(result.status, "completed")
        self.assertEqual(result.step_results[0].tool_calls, 1)
        self.assertEqual(len(result.step_results[0].observations), 1)
        self.assertEqual(
            result.step_results[0].observations[0].result["status"],
            "success",
        )
        self.assertIn("总目标", decision_client.requests[0].system_instructions)

    def test_graph_contains_explicit_business_nodes(self) -> None:
        workflow = self.build_workflow(
            planner=ScriptedPlanner([make_plan("locate")]),
            executor=ScriptedStepExecutor([completed("locate")]),
            evaluator=ScriptedEvaluator([passed_evaluation()]),
        )

        nodes = set(workflow.graph.get_graph().nodes)

        self.assertTrue(
            {"plan", "execute_step", "evaluate", "reflect", "replan", "report"}
            <= nodes
        )


if __name__ == "__main__":
    unittest.main()
