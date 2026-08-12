"""带预算、重复检测和错误停止条件的最小 ReAct 控制循环。"""

from __future__ import annotations

from dataclasses import asdict, dataclass, is_dataclass
from collections import Counter
from enum import Enum
import hashlib
import json
from pathlib import Path
from typing import Any, Callable, Iterable, Literal, Mapping

from pydantic import BaseModel

from repo_agent.tools.models import ToolResult
from repo_agent.tools.registry import ToolRegistry

from .model import (
    DecisionModel,
    FinalAnswerDecision,
    ModelDecisionError,
    ModelObservation,
    ModelRequest,
    ToolCallDecision,
)


def _to_jsonable(value: Any) -> Any:
    """把工具结果转换成可发送给模型的普通数据。"""

    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if is_dataclass(value) and not isinstance(value, type):
        return {key: _to_jsonable(item) for key, item in asdict(value).items()}
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _to_jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list, set)):
        return [_to_jsonable(item) for item in value]
    return value


def _result_to_model_dict(result: ToolResult[Any]) -> dict[str, Any]:
    """保留工具状态、数据、错误和元数据四个维度。"""

    return {
        "status": result.status,
        "data": _to_jsonable(result.data),
        "error": _to_jsonable(result.error),
        "metadata": _to_jsonable(result.metadata),
    }


def _call_fingerprint(tool_name: str, arguments: Mapping[str, Any]) -> str:
    """为完全相同的工具调用生成稳定指纹。"""

    payload = json.dumps(
        {"tool_name": tool_name, "arguments": _to_jsonable(arguments)},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ReActConfig:
    """最小 ReAct 循环的预算与停止策略。"""

    max_iterations: int = 8
    max_tool_calls: int = 6
    max_consecutive_tool_errors: int = 2
    max_identical_tool_calls: int = 1

    def __post_init__(self) -> None:
        if self.max_iterations < 1:
            raise ValueError("max_iterations 必须大于等于 1")
        if self.max_tool_calls < 0:
            raise ValueError("max_tool_calls 必须大于等于 0")
        if self.max_consecutive_tool_errors < 1:
            raise ValueError("max_consecutive_tool_errors 必须大于等于 1")
        if self.max_identical_tool_calls < 1:
            raise ValueError("max_identical_tool_calls 必须大于等于 1")


@dataclass(frozen=True, slots=True)
class ReActEvent:
    """一次已执行工具调用的可审计事件。"""

    iteration: int
    tool_name: str
    arguments: dict[str, Any]
    decision_summary: str
    call_fingerprint: str
    result: ToolResult[Any]

    def to_model_observation(self) -> ModelObservation:
        """转换成下一轮模型可读取的 Observation。"""

        return ModelObservation(
            iteration=self.iteration,
            tool_name=self.tool_name,
            arguments=self.arguments,
            result=_result_to_model_dict(self.result),
            decision_summary=self.decision_summary,
        )


@dataclass(frozen=True, slots=True)
class ReActRunResult:
    """一次最小 ReAct 运行的最终状态。"""

    status: Literal[
        "completed",
        "budget_exhausted",
        "duplicate_call_stopped",
        "tool_error_stopped",
        "model_error",
    ]
    final_answer: str | None
    final_decision_summary: str | None
    events: tuple[ReActEvent, ...]
    iterations: int
    tool_calls: int
    stop_reason: str


class ReActExecutor:
    """在确定性边界内编排模型决策和工具观察。"""

    def __init__(
        self,
        decision_model: DecisionModel,
        tool_registry: ToolRegistry,
        *,
        config: ReActConfig | None = None,
        progress_callback: Callable[[str], None] | None = None,
    ) -> None:
        self.decision_model = decision_model
        self.tool_registry = tool_registry
        self.config = config or ReActConfig()
        self.progress_callback = progress_callback

    def _emit(self, message: str) -> None:
        """发送不含工具参数和仓库内容的实时进度事件。"""

        if self.progress_callback is not None:
            self.progress_callback(message)

    def run(
        self,
        user_goal: str,
        *,
        system_instructions: str = "",
        allowed_tools: Iterable[str] | None = None,
        skill_instructions: Iterable[str] = (),
        required_tools: Iterable[str] = (),
        required_tool_counts: Mapping[str, int] | None = None,
    ) -> ReActRunResult:
        """运行 ReAct 循环，直到完成或触发停止条件。"""

        if not user_goal.strip():
            raise ValueError("user_goal 不能为空")

        normalized_allowed = (
            tuple(sorted(set(allowed_tools))) if allowed_tools is not None else None
        )
        normalized_skill_instructions = tuple(
            instruction.strip()
            for instruction in skill_instructions
            if instruction.strip()
        )
        available_tools = self.tool_registry.model_tools(normalized_allowed)
        available_tool_names = {tool.name for tool in available_tools}
        normalized_required_counts = Counter(required_tools)
        for tool_name, count in (required_tool_counts or {}).items():
            if count < 1:
                raise ValueError("必需工具调用次数必须大于等于 1")
            normalized_required_counts[tool_name] = max(
                normalized_required_counts[tool_name],
                count,
            )
        normalized_required = tuple(sorted(normalized_required_counts))
        unavailable_required = sorted(set(normalized_required) - available_tool_names)
        if unavailable_required:
            raise ValueError(
                "必需工具未暴露给当前 ReAct 运行：" + ", ".join(unavailable_required)
            )
        events: list[ReActEvent] = []
        fingerprint_counts: dict[str, int] = {}
        consecutive_tool_errors = 0
        tool_calls = 0
        successful_tool_counts: Counter[str] = Counter()
        completion_gate_feedback = ""
        self._emit("ReAct 开始处理当前计划步骤")

        for iteration in range(1, self.config.max_iterations + 1):
            self._emit(f"ReAct 第 {iteration} 轮：请求模型决策")
            request = ModelRequest(
                user_goal=user_goal,
                system_instructions=system_instructions,
                available_tools=available_tools,
                observations=tuple(event.to_model_observation() for event in events),
                remaining_iterations=self.config.max_iterations - iteration + 1,
                remaining_tool_calls=self.config.max_tool_calls - tool_calls,
                skill_instructions=normalized_skill_instructions,
                remaining_required_tools=tuple(
                    tool
                    for tool in normalized_required
                    for _ in range(
                        max(
                            0,
                            normalized_required_counts[tool]
                            - successful_tool_counts[tool],
                        )
                    )
                ),
                completion_gate_feedback=completion_gate_feedback,
            )
            try:
                decision = self.decision_model.decide(request)
            except ModelDecisionError as exc:
                self._emit(f"ReAct 第 {iteration} 轮：模型决策失败")
                return ReActRunResult(
                    status="model_error",
                    final_answer=None,
                    final_decision_summary=None,
                    events=tuple(events),
                    iterations=iteration,
                    tool_calls=tool_calls,
                    stop_reason=str(exc),
                )

            if isinstance(decision, FinalAnswerDecision):
                missing_required = tuple(
                    tool
                    for tool in normalized_required
                    for _ in range(
                        max(
                            0,
                            normalized_required_counts[tool]
                            - successful_tool_counts[tool],
                        )
                    )
                )
                if missing_required:
                    if (
                        tool_calls >= self.config.max_tool_calls
                        or iteration >= self.config.max_iterations
                    ):
                        return ReActRunResult(
                            status="budget_exhausted",
                            final_answer=None,
                            final_decision_summary=None,
                            events=tuple(events),
                            iterations=iteration,
                            tool_calls=tool_calls,
                            stop_reason=(
                                "必需工具尚未成功调用：" + ", ".join(missing_required)
                            ),
                        )
                    self._emit(
                        "ReAct 拒绝提前结束；尚未调用必需工具："
                        + ", ".join(missing_required)
                    )
                    completion_gate_feedback = (
                        "上一轮 final_answer 已被运行时拒绝。不要再次返回 final_answer；"
                        "下一轮必须调用尚未完成的必需工具："
                        + ", ".join(missing_required)
                    )
                    continue
                self._emit(f"ReAct 第 {iteration} 轮：形成步骤结论")
                return ReActRunResult(
                    status="completed",
                    final_answer=decision.answer,
                    final_decision_summary=decision.decision_summary,
                    events=tuple(events),
                    iterations=iteration,
                    tool_calls=tool_calls,
                    stop_reason="模型返回最终答案",
                )

            if not isinstance(decision, ToolCallDecision):
                return ReActRunResult(
                    status="model_error",
                    final_answer=None,
                    final_decision_summary=None,
                    events=tuple(events),
                    iterations=iteration,
                    tool_calls=tool_calls,
                    stop_reason="模型返回了未知决策类型",
                )

            if tool_calls >= self.config.max_tool_calls:
                self._emit("ReAct 工具调用预算已耗尽")
                return ReActRunResult(
                    status="budget_exhausted",
                    final_answer=None,
                    final_decision_summary=None,
                    events=tuple(events),
                    iterations=iteration,
                    tool_calls=tool_calls,
                    stop_reason="工具调用预算已耗尽",
                )

            fingerprint = _call_fingerprint(decision.tool_name, decision.arguments)
            previous_count = fingerprint_counts.get(fingerprint, 0)
            if previous_count >= self.config.max_identical_tool_calls:
                return ReActRunResult(
                    status="duplicate_call_stopped",
                    final_answer=None,
                    final_decision_summary=None,
                    events=tuple(events),
                    iterations=iteration,
                    tool_calls=tool_calls,
                    stop_reason=f"检测到重复工具调用：{decision.tool_name}",
                )
            fingerprint_counts[fingerprint] = previous_count + 1

            self._emit(
                f"ReAct 第 {iteration} 轮：调用工具 {decision.tool_name}"
            )
            result = self.tool_registry.dispatch(
                decision.tool_name,
                decision.arguments,
                allowed_tools=normalized_allowed,
            )
            tool_calls += 1
            events.append(
                ReActEvent(
                    iteration=iteration,
                    tool_name=decision.tool_name,
                    arguments=dict(decision.arguments),
                    decision_summary=decision.decision_summary,
                    call_fingerprint=fingerprint,
                    result=result,
                )
            )
            self._emit(
                f"工具 {decision.tool_name} 返回"
                f"{'成功' if result.ok else '错误'}"
            )

            if result.ok:
                consecutive_tool_errors = 0
                successful_tool_counts[decision.tool_name] += 1
            else:
                consecutive_tool_errors += 1
                if (
                    consecutive_tool_errors
                    >= self.config.max_consecutive_tool_errors
                ):
                    return ReActRunResult(
                        status="tool_error_stopped",
                        final_answer=None,
                        final_decision_summary=None,
                        events=tuple(events),
                        iterations=iteration,
                        tool_calls=tool_calls,
                        stop_reason="连续工具错误达到上限",
                    )

        return ReActRunResult(
            status="budget_exhausted",
            final_answer=None,
            final_decision_summary=None,
            events=tuple(events),
            iterations=self.config.max_iterations,
            tool_calls=tool_calls,
            stop_reason="迭代预算已耗尽",
        )
