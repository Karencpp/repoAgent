"""工作流节点使用的确定性测试替身。"""

from __future__ import annotations

from collections import deque
from typing import Mapping, Sequence, TypeVar

from pydantic import BaseModel

from .models import EvaluationResult, ExecutionPlan, ReflectionResult, StepExecution
from .ports import (
    EvaluationRequest,
    PlanningRequest,
    ReflectionRequest,
    ReplanningRequest,
    StepExecutionRequest,
)


ModelT = TypeVar("ModelT", bound=BaseModel)


def _next_model(
    responses: deque[Mapping[str, object] | ModelT | Exception],
    model_type: type[ModelT],
    empty_message: str,
) -> ModelT:
    """从脚本队列读取并校验下一条响应。"""

    if not responses:
        raise RuntimeError(empty_message)
    response = responses.popleft()
    if isinstance(response, Exception):
        raise response
    return model_type.model_validate(response)


class ScriptedPlanner:
    """分别记录初次规划和重规划请求的脚本 Planner。"""

    def __init__(
        self,
        initial_plans: Sequence[Mapping[str, object] | ExecutionPlan | Exception],
        replans: Sequence[Mapping[str, object] | ExecutionPlan | Exception] = (),
    ) -> None:
        self._initial_plans = deque(initial_plans)
        self._replans = deque(replans)
        self.planning_requests: list[PlanningRequest] = []
        self.replanning_requests: list[ReplanningRequest] = []

    def create_plan(self, request: PlanningRequest) -> ExecutionPlan:
        """返回下一份初始计划。"""

        self.planning_requests.append(request)
        return _next_model(
            self._initial_plans,
            ExecutionPlan,
            "脚本 Planner 没有剩余初始计划",
        )

    def replan(self, request: ReplanningRequest) -> ExecutionPlan:
        """返回下一份修订计划。"""

        self.replanning_requests.append(request)
        return _next_model(
            self._replans,
            ExecutionPlan,
            "脚本 Planner 没有剩余修订计划",
        )


class ScriptedStepExecutor:
    """按顺序返回步骤结果的脚本执行器。"""

    def __init__(
        self,
        responses: Sequence[Mapping[str, object] | StepExecution | Exception],
    ) -> None:
        self._responses = deque(responses)
        self.requests: list[StepExecutionRequest] = []

    def execute(self, request: StepExecutionRequest) -> StepExecution:
        """记录请求并返回下一条步骤结果。"""

        self.requests.append(request)
        return _next_model(
            self._responses,
            StepExecution,
            "脚本执行器没有剩余步骤结果",
        )


class ScriptedEvaluator:
    """按顺序返回客观评估结果的脚本 Evaluator。"""

    def __init__(
        self,
        responses: Sequence[Mapping[str, object] | EvaluationResult | Exception],
    ) -> None:
        self._responses = deque(responses)
        self.requests: list[EvaluationRequest] = []

    def evaluate(self, request: EvaluationRequest) -> EvaluationResult:
        """记录请求并返回下一条评估。"""

        self.requests.append(request)
        return _next_model(
            self._responses,
            EvaluationResult,
            "脚本 Evaluator 没有剩余评估结果",
        )


class ScriptedReflector:
    """按顺序返回修正策略的脚本 Reflector。"""

    def __init__(
        self,
        responses: Sequence[Mapping[str, object] | ReflectionResult | Exception],
    ) -> None:
        self._responses = deque(responses)
        self.requests: list[ReflectionRequest] = []

    def reflect(self, request: ReflectionRequest) -> ReflectionResult:
        """记录请求并返回下一条反思结果。"""

        self.requests.append(request)
        return _next_model(
            self._responses,
            ReflectionResult,
            "脚本 Reflector 没有剩余反思结果",
        )
