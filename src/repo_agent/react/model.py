"""ReAct 循环使用的结构化模型决策边界。"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Annotated, Any, Literal, Mapping, Protocol, Sequence

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from repo_agent.tools.registry import ModelToolDefinition


class ToolCallDecision(BaseModel):
    """模型请求调用一个工具。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    type: Literal["tool_call"]
    tool_name: str = Field(min_length=1, max_length=100)
    arguments: dict[str, Any]
    decision_summary: str = Field(min_length=1, max_length=500)


class FinalAnswerDecision(BaseModel):
    """模型认为当前任务可以结束。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    type: Literal["final_answer"]
    answer: str = Field(min_length=1, max_length=20_000)
    decision_summary: str = Field(min_length=1, max_length=500)


AgentDecision = Annotated[
    ToolCallDecision | FinalAnswerDecision,
    Field(discriminator="type"),
]
DECISION_ADAPTER = TypeAdapter(AgentDecision)


@dataclass(frozen=True, slots=True)
class ModelObservation:
    """提供给下一轮模型的结构化工具观察。"""

    iteration: int
    tool_name: str
    arguments: dict[str, Any]
    result: dict[str, Any]
    decision_summary: str


@dataclass(frozen=True, slots=True)
class ModelRequest:
    """一次模型决策所需的最小请求。"""

    user_goal: str
    system_instructions: str
    available_tools: tuple[ModelToolDefinition, ...]
    observations: tuple[ModelObservation, ...]
    remaining_iterations: int
    remaining_tool_calls: int
    skill_instructions: tuple[str, ...] = ()
    remaining_required_tools: tuple[str, ...] = ()
    completion_gate_feedback: str = ""


class RawDecisionClient(Protocol):
    """模型提供商适配器需要实现的原始决策接口。"""

    def generate_decision(self, request: ModelRequest) -> Mapping[str, Any]:
        """返回尚未经过本地校验的结构化决策。"""


class DecisionModel(Protocol):
    """ReAct 执行器依赖的已校验决策接口。"""

    def decide(self, request: ModelRequest) -> ToolCallDecision | FinalAnswerDecision:
        """返回通过 Schema 校验的 Agent 决策。"""


class ModelDecisionError(RuntimeError):
    """模型调用或结构化输出校验失败。"""


class StructuredDecisionModel:
    """把模型原始映射转换成严格的判别联合类型。"""

    def __init__(
        self,
        client: RawDecisionClient,
        *,
        max_validation_attempts: int = 1,
    ) -> None:
        if max_validation_attempts < 1:
            raise ValueError("max_validation_attempts 必须大于等于 1")
        self.client = client
        self.max_validation_attempts = max_validation_attempts

    def decide(self, request: ModelRequest) -> ToolCallDecision | FinalAnswerDecision:
        """调用模型并校验决策结构。"""

        last_error: ValidationError | None = None
        for _ in range(self.max_validation_attempts):
            try:
                raw_decision = self.client.generate_decision(request)
            except Exception as exc:
                raise ModelDecisionError(
                    f"模型调用失败：{type(exc).__name__}: {exc}"
                ) from exc
            try:
                return DECISION_ADAPTER.validate_python(raw_decision)
            except ValidationError as exc:
                last_error = exc
        assert last_error is not None
        details = "; ".join(
            f"{'.'.join(str(item) for item in error['loc']) or '<root>'}: "
            f"{error['msg']}"
            for error in last_error.errors(include_url=False)[:3]
        )
        raise ModelDecisionError(
            "模型决策不满足结构约束："
            f"{last_error.error_count()} 个错误；{details}"
        ) from last_error


class ScriptedDecisionClient:
    """按预设顺序返回决策的确定性测试模型。"""

    def __init__(
        self,
        responses: Sequence[Mapping[str, Any] | Exception],
    ) -> None:
        self._responses = deque(responses)
        self.requests: list[ModelRequest] = []

    def generate_decision(self, request: ModelRequest) -> Mapping[str, Any]:
        """记录请求并返回下一条脚本响应。"""

        self.requests.append(request)
        if not self._responses:
            raise RuntimeError("脚本模型没有剩余响应")
        response = self._responses.popleft()
        if isinstance(response, Exception):
            raise response
        return response
