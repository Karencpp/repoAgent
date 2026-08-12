"""LangGraph 主闭环使用的结构化领域模型。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from repo_agent.react import ReActRunResult


class WorkflowModel(BaseModel):
    """工作流模型的公共严格配置。"""

    model_config = ConfigDict(extra="forbid", frozen=True)


class PlanStep(WorkflowModel):
    """一条可执行、可验证的计划步骤。"""

    id: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_-]+$")
    goal: str = Field(min_length=1, max_length=1_000)
    expected_evidence: tuple[str, ...] = Field(min_length=1, max_length=10)
    allowed_tools: tuple[str, ...] = Field(min_length=1, max_length=10)
    status: Literal["pending", "running", "completed", "failed", "skipped"] = (
        "pending"
    )
    result_summary: str | None = Field(default=None, max_length=2_000)
    attempts: int = Field(default=0, ge=0, le=10)


class ExecutionPlan(WorkflowModel):
    """Planner 生成的有限步骤计划。"""

    rationale: str = Field(min_length=1, max_length=2_000)
    steps: tuple[PlanStep, ...] = Field(min_length=1, max_length=6)

    @model_validator(mode="after")
    def validate_unique_step_ids(self) -> "ExecutionPlan":
        """拒绝重复步骤标识，避免状态更新命中错误步骤。"""

        step_ids = [step.id for step in self.steps]
        if len(step_ids) != len(set(step_ids)):
            raise ValueError("计划步骤 id 不能重复")
        return self


class StepToolObservation(WorkflowModel):
    """从 ReAct 事件提取的可持久化工具观察。"""

    iteration: int = Field(ge=1)
    tool_name: str = Field(min_length=1, max_length=100)
    arguments: dict[str, Any]
    decision_summary: str = Field(min_length=1, max_length=500)
    result: dict[str, Any]


class StepExecution(WorkflowModel):
    """一个计划步骤的执行结果。"""

    step_id: str = Field(min_length=1, max_length=100)
    execution_key: str | None = Field(default=None, min_length=1, max_length=100)
    status: Literal["completed", "failed"]
    summary: str = Field(min_length=1, max_length=20_000)
    react_status: Literal[
        "completed",
        "budget_exhausted",
        "duplicate_call_stopped",
        "tool_error_stopped",
        "model_error",
        "executor_error",
    ]
    stop_reason: str = Field(min_length=1, max_length=2_000)
    iterations: int = Field(ge=0)
    tool_calls: int = Field(ge=0)
    observations: tuple[StepToolObservation, ...] = ()
    active_skill_name: str | None = Field(default=None, max_length=64)
    active_skill_version: str | None = Field(default=None, max_length=100)
    active_skill_hash: str | None = Field(
        default=None,
        min_length=64,
        max_length=64,
    )
    skill_route_reasons: tuple[str, ...] = Field(default=(), max_length=20)

    @classmethod
    def from_react_result(
        cls,
        step_id: str,
        result: ReActRunResult,
    ) -> "StepExecution":
        """把局部 ReAct 结果压缩成 Graph 可持久化状态。"""

        observations = tuple(
            StepToolObservation(
                iteration=event.iteration,
                tool_name=event.tool_name,
                arguments=event.arguments,
                decision_summary=event.decision_summary,
                result=event.to_model_observation().result,
            )
            for event in result.events
        )
        completed = result.status == "completed"
        return cls(
            step_id=step_id,
            status="completed" if completed else "failed",
            summary=result.final_answer or result.stop_reason,
            react_status=result.status,
            stop_reason=result.stop_reason,
            iterations=result.iterations,
            tool_calls=result.tool_calls,
            observations=observations,
        )


class EvaluationResult(WorkflowModel):
    """Evaluator 对当前计划执行结果的客观评估。"""

    passed: bool
    summary: str = Field(min_length=1, max_length=5_000)
    issues: tuple[str, ...] = Field(default=(), max_length=20)
    evidence: tuple[str, ...] = Field(default=(), max_length=20)


class ReflectionResult(WorkflowModel):
    """失败后对原因和下一策略的结构化判断。"""

    failure_cause: str = Field(min_length=1, max_length=2_000)
    corrective_action: str = Field(min_length=1, max_length=2_000)
    should_replan: bool


class GraphTraceEvent(WorkflowModel):
    """记录节点状态变化而不保存完整隐藏思维链。"""

    node: Literal["plan", "execute_step", "evaluate", "reflect", "replan", "report"]
    event: str = Field(min_length=1, max_length=100)
    summary: str = Field(min_length=1, max_length=2_000)


class RepoAgentRunResult(WorkflowModel):
    """LangGraph 主闭环对外返回的稳定结果。"""

    run_id: str
    thread_id: str
    project_id: str
    repo_root: str
    repo_revision: str
    user_goal: str
    mode: Literal["diagnose", "fix"]
    status: Literal["completed", "failed", "interrupted"]
    plan: ExecutionPlan | None
    plan_history: tuple[ExecutionPlan, ...]
    step_results: tuple[StepExecution, ...]
    evaluation: EvaluationResult | None
    evaluation_history: tuple[EvaluationResult, ...]
    reflection_history: tuple[ReflectionResult, ...]
    reflection_count: int = Field(ge=0)
    replan_count: int = Field(ge=0)
    final_report: str
    stop_reason: str
    trace: tuple[GraphTraceEvent, ...]
