"""主闭环依赖的 Planner、Executor、Evaluator 和 Reflector 端口。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

from repo_agent.react import ReActExecutor

from .models import (
    EvaluationResult,
    ExecutionPlan,
    PlanStep,
    ReflectionResult,
    StepExecution,
)


@dataclass(frozen=True, slots=True)
class PlanningRequest:
    """初次规划所需的最小上下文。"""

    project_id: str
    repo_root: str
    repo_revision: str
    user_goal: str
    mode: Literal["diagnose", "fix"]


@dataclass(frozen=True, slots=True)
class StepExecutionRequest:
    """执行当前计划步骤所需的上下文。"""

    run_id: str
    execution_key: str
    user_goal: str
    step: PlanStep
    previous_results: tuple[StepExecution, ...]
    latest_reflection: ReflectionResult | None


@dataclass(frozen=True, slots=True)
class EvaluationRequest:
    """评估计划执行结果所需的证据。"""

    run_id: str
    project_id: str
    repo_revision: str
    user_goal: str
    plan: ExecutionPlan
    step_results: tuple[StepExecution, ...]
    mode: Literal["diagnose", "fix"]


@dataclass(frozen=True, slots=True)
class ReflectionRequest:
    """解释失败并选择重试或重规划所需的上下文。"""

    user_goal: str
    plan: ExecutionPlan
    step_results: tuple[StepExecution, ...]
    evaluation: EvaluationResult


@dataclass(frozen=True, slots=True)
class ReplanningRequest:
    """生成修订计划所需的失败反馈。"""

    user_goal: str
    previous_plan: ExecutionPlan
    step_results: tuple[StepExecution, ...]
    evaluation: EvaluationResult
    reflection: ReflectionResult


class PlannerPort(Protocol):
    """初次规划和重规划的统一端口。"""

    def create_plan(self, request: PlanningRequest) -> ExecutionPlan:
        """创建第一版有限步骤计划。"""

    def replan(self, request: ReplanningRequest) -> ExecutionPlan:
        """根据失败证据创建替代步骤。"""


class StepExecutorPort(Protocol):
    """执行单个计划步骤的端口。"""

    def execute(self, request: StepExecutionRequest) -> StepExecution:
        """执行当前步骤并返回可持久化结果。"""


class EvaluatorPort(Protocol):
    """使用外部证据判断任务是否通过。"""

    def evaluate(self, request: EvaluationRequest) -> EvaluationResult:
        """返回任务级评估结果。"""


class ReflectorPort(Protocol):
    """分析失败原因并决定局部重试或重规划。"""

    def reflect(self, request: ReflectionRequest) -> ReflectionResult:
        """返回一次受限反思结果。"""


class ReActStepExecutor:
    """把上一模块的 ReActExecutor 适配成计划步骤执行器。"""

    def __init__(self, executor: ReActExecutor) -> None:
        self.executor = executor

    def execute(self, request: StepExecutionRequest) -> StepExecution:
        """用当前步骤的工具白名单运行局部 ReAct 循环。"""

        step = request.step
        previous = "\n".join(
            f"- {result.step_id}: {result.summary}"
            for result in request.previous_results
        )
        reflection = (
            request.latest_reflection.corrective_action
            if request.latest_reflection is not None
            else "无"
        )
        system_instructions = (
            f"总目标：{request.user_goal}\n"
            f"当前步骤：{step.goal}\n"
            f"已有步骤结果：\n{previous or '无'}\n"
            f"最近修正建议：{reflection}\n"
            "只完成当前步骤；证据足够时返回简洁的步骤结论。"
        )
        result = self.executor.run(
            step.goal,
            system_instructions=system_instructions,
            allowed_tools=step.allowed_tools,
        )
        return StepExecution.from_react_result(step.id, result).model_copy(
            update={"execution_key": request.execution_key}
        )
