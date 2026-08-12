"""Plan、Execute、Evaluate、Reflect 和 Replan 的 LangGraph 主闭环。"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import operator
from typing import Annotated, Any, Callable, Literal, TypedDict
from uuid import uuid4

from langgraph.errors import GraphRecursionError
from langgraph.graph import END, START, StateGraph
from langgraph.checkpoint.base import BaseCheckpointSaver

from repo_agent.projects import ProjectContext

from .models import (
    EvaluationResult,
    ExecutionPlan,
    GraphTraceEvent,
    PlanStep,
    ReflectionResult,
    RepoAgentRunResult,
    StepExecution,
)
from .ports import (
    EvaluationRequest,
    EvaluatorPort,
    PlannerPort,
    PlanningRequest,
    ReflectionRequest,
    ReflectorPort,
    ReplanningRequest,
    StepExecutionRequest,
    StepExecutorPort,
)


class RepoAgentGraphState(TypedDict):
    """每个节点共享、每次只做局部更新的显式状态。"""

    run_id: str
    thread_id: str
    project_id: str
    repo_root: str
    repo_revision: str
    user_goal: str
    mode: Literal["diagnose", "fix"]
    plan: ExecutionPlan | None
    plan_history: Annotated[list[ExecutionPlan], operator.add]
    current_step_index: int
    step_results: Annotated[list[StepExecution], operator.add]
    evaluation: EvaluationResult | None
    evaluation_history: Annotated[list[EvaluationResult], operator.add]
    reflection: ReflectionResult | None
    reflection_history: Annotated[list[ReflectionResult], operator.add]
    reflection_count: int
    replan_count: int
    status: Literal["running", "completed", "failed"]
    stop_reason: str
    final_report: str
    trace: Annotated[list[GraphTraceEvent], operator.add]


@dataclass(frozen=True, slots=True)
class WorkflowConfig:
    """外层 Graph 的反思、重规划和递归安全上限。"""

    max_reflections: int = 1
    max_replans: int = 1
    recursion_limit: int = 40

    def __post_init__(self) -> None:
        if self.max_reflections < 0:
            raise ValueError("max_reflections 必须大于等于 0")
        if self.max_replans < 0:
            raise ValueError("max_replans 必须大于等于 0")
        if self.recursion_limit < 5:
            raise ValueError("recursion_limit 必须大于等于 5")


def _trace(
    node: Literal["plan", "execute_step", "evaluate", "reflect", "replan", "report"],
    event: str,
    summary: str,
) -> list[GraphTraceEvent]:
    """构造一条追加到 reducer 字段的节点事件。"""

    return [GraphTraceEvent(node=node, event=event, summary=summary)]


def _normalize_plan(plan: ExecutionPlan) -> ExecutionPlan:
    """将模型返回的步骤统一重置为尚未执行。"""

    return ExecutionPlan(
        rationale=plan.rationale,
        steps=tuple(
            step.model_copy(
                update={
                    "status": "pending",
                    "result_summary": None,
                    "attempts": 0,
                }
            )
            for step in plan.steps
        ),
    )


def _replace_step(
    plan: ExecutionPlan,
    index: int,
    step: PlanStep,
) -> ExecutionPlan:
    """不可变地替换计划中的一个步骤。"""

    steps = list(plan.steps)
    steps[index] = step
    return plan.model_copy(update={"steps": tuple(steps)})


def _execution_key(run_id: str, step_id: str, attempt: int) -> str:
    """为可能重放的步骤尝试生成稳定幂等键。"""

    payload = f"{run_id}\x00{step_id}\x00{attempt}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class RepoAgentWorkflow:
    """持有节点依赖并编译可执行 LangGraph。"""

    def __init__(
        self,
        planner: PlannerPort,
        step_executor: StepExecutorPort,
        evaluator: EvaluatorPort,
        reflector: ReflectorPort,
        *,
        config: WorkflowConfig | None = None,
        checkpointer: BaseCheckpointSaver[Any] | None = None,
        interrupt_before: tuple[str, ...] = (),
        progress_callback: Callable[[str], None] | None = None,
    ) -> None:
        self.planner = planner
        self.step_executor = step_executor
        self.evaluator = evaluator
        self.reflector = reflector
        self.config = config or WorkflowConfig()
        self.checkpointer = checkpointer
        self.interrupt_before = interrupt_before
        self.progress_callback = progress_callback
        self.graph = self._build_graph()

    def _emit(self, message: str) -> None:
        """向交互层发送不参与状态持久化的实时进度。"""

        if self.progress_callback is not None:
            self.progress_callback(message)

    def _build_graph(self):
        """声明节点、静态边和条件路由，再编译状态图。"""

        builder = StateGraph(RepoAgentGraphState)
        builder.add_node("plan", self._plan_node)
        builder.add_node("execute_step", self._execute_step_node)
        builder.add_node("evaluate", self._evaluate_node)
        builder.add_node("reflect", self._reflect_node)
        builder.add_node("replan", self._replan_node)
        builder.add_node("report", self._report_node)

        builder.add_edge(START, "plan")
        builder.add_conditional_edges(
            "plan",
            self._route_after_plan,
            {"execute_step": "execute_step", "report": "report"},
        )
        builder.add_conditional_edges(
            "execute_step",
            self._route_after_execute,
            {"execute_step": "execute_step", "evaluate": "evaluate"},
        )
        builder.add_conditional_edges(
            "evaluate",
            self._route_after_evaluate,
            {"reflect": "reflect", "report": "report"},
        )
        builder.add_conditional_edges(
            "reflect",
            self._route_after_reflect,
            {
                "execute_step": "execute_step",
                "replan": "replan",
                "report": "report",
            },
        )
        builder.add_conditional_edges(
            "replan",
            self._route_after_replan,
            {"execute_step": "execute_step", "report": "report"},
        )
        builder.add_edge("report", END)
        return builder.compile(
            checkpointer=self.checkpointer,
            interrupt_before=list(self.interrupt_before) or None,
            name="repo-agent-main-workflow",
        )

    def _plan_node(self, state: RepoAgentGraphState) -> dict[str, object]:
        """创建第一版计划，并把供应商异常收敛到状态。"""

        self._emit("Planner 正在生成结构化计划")
        request = PlanningRequest(
            project_id=state["project_id"],
            repo_root=state["repo_root"],
            repo_revision=state["repo_revision"],
            user_goal=state["user_goal"],
            mode=state["mode"],
        )
        try:
            plan = _normalize_plan(self.planner.create_plan(request))
        except Exception as exc:
            self._emit("Planner 生成计划失败")
            reason = f"规划失败：{type(exc).__name__}: {exc}"
            return {
                "status": "failed",
                "stop_reason": reason,
                "trace": _trace("plan", "failed", reason),
            }
        self._emit(f"Planner 已生成 {len(plan.steps)} 个步骤")
        return {
            "plan": plan,
            "plan_history": [plan],
            "current_step_index": 0,
            "trace": _trace(
                "plan",
                "created",
                f"生成 {len(plan.steps)} 个计划步骤",
            ),
        }

    def _execute_step_node(self, state: RepoAgentGraphState) -> dict[str, object]:
        """执行一个步骤，并显式更新步骤状态和索引。"""

        plan = state["plan"]
        index = state["current_step_index"]
        if plan is None or index >= len(plan.steps):
            reason = "执行节点找不到当前计划步骤"
            return {
                "status": "failed",
                "stop_reason": reason,
                "trace": _trace("execute_step", "failed", reason),
            }

        step = plan.steps[index]
        self._emit(
            f"开始执行步骤 {index + 1}/{len(plan.steps)}：{step.goal}"
        )
        running_step = step.model_copy(
            update={"status": "running", "attempts": step.attempts + 1}
        )
        execution_key = _execution_key(
            state["run_id"],
            step.id,
            running_step.attempts,
        )
        running_plan = _replace_step(plan, index, running_step)
        request = StepExecutionRequest(
            run_id=state["run_id"],
            execution_key=execution_key,
            user_goal=state["user_goal"],
            step=running_step,
            previous_results=tuple(state["step_results"]),
            latest_reflection=state["reflection"],
        )
        try:
            execution = self.step_executor.execute(request)
            if execution.step_id != step.id:
                raise ValueError(
                    f"步骤结果 id 不匹配：期望 {step.id}，得到 {execution.step_id}"
                )
            execution = execution.model_copy(
                update={"execution_key": execution_key}
            )
        except Exception as exc:
            reason = f"步骤执行器失败：{type(exc).__name__}: {exc}"
            execution = StepExecution(
                step_id=step.id,
                execution_key=execution_key,
                status="failed",
                summary=reason,
                react_status="executor_error",
                stop_reason=reason,
                iterations=0,
                tool_calls=0,
            )

        final_step = running_step.model_copy(
            update={
                "status": execution.status,
                "result_summary": execution.summary,
            }
        )
        updated_plan = _replace_step(running_plan, index, final_step)
        self._emit(
            f"步骤 {step.id} {'完成' if execution.status == 'completed' else '失败'}："
            f"{execution.summary[:200]}"
        )
        next_index = index + 1 if execution.status == "completed" else index
        return {
            "plan": updated_plan,
            "current_step_index": next_index,
            "step_results": [execution],
            "trace": _trace(
                "execute_step",
                execution.status,
                f"步骤 {step.id}：{execution.summary}",
            ),
        }

    def _evaluate_node(self, state: RepoAgentGraphState) -> dict[str, object]:
        """在步骤结束或失败后进行任务级客观评估。"""

        self._emit("Evaluator 正在检查步骤状态和工具证据")
        plan = state["plan"]
        if plan is None:
            reason = "评估节点缺少执行计划"
            return {
                "status": "failed",
                "stop_reason": reason,
                "trace": _trace("evaluate", "failed", reason),
            }
        request = EvaluationRequest(
            run_id=state["run_id"],
            project_id=state["project_id"],
            repo_revision=state["repo_revision"],
            user_goal=state["user_goal"],
            plan=plan,
            step_results=tuple(state["step_results"]),
            mode=state["mode"],
        )
        try:
            evaluation = self.evaluator.evaluate(request)
        except Exception as exc:
            reason = f"评估失败：{type(exc).__name__}: {exc}"
            return {
                "status": "failed",
                "stop_reason": reason,
                "trace": _trace("evaluate", "failed", reason),
            }
        self._emit(
            f"Evaluator {'通过' if evaluation.passed else '拒绝'}："
            f"{evaluation.summary}"
        )
        return {
            "evaluation": evaluation,
            "evaluation_history": [evaluation],
            "trace": _trace(
                "evaluate",
                "passed" if evaluation.passed else "rejected",
                evaluation.summary,
            ),
        }

    def _reflect_node(self, state: RepoAgentGraphState) -> dict[str, object]:
        """只在评估失败且预算允许时分析一次失败原因。"""

        self._emit("Reflector 正在分析失败原因")
        plan = state["plan"]
        evaluation = state["evaluation"]
        if plan is None or evaluation is None:
            reason = "反思节点缺少计划或评估结果"
            return {
                "status": "failed",
                "stop_reason": reason,
                "trace": _trace("reflect", "failed", reason),
            }
        request = ReflectionRequest(
            user_goal=state["user_goal"],
            plan=plan,
            step_results=tuple(state["step_results"]),
            evaluation=evaluation,
        )
        try:
            reflection = self.reflector.reflect(request)
        except Exception as exc:
            reason = f"反思失败：{type(exc).__name__}: {exc}"
            return {
                "status": "failed",
                "stop_reason": reason,
                "reflection_count": state["reflection_count"] + 1,
                "trace": _trace("reflect", "failed", reason),
            }

        updates: dict[str, object] = {
            "reflection": reflection,
            "reflection_history": [reflection],
            "reflection_count": state["reflection_count"] + 1,
            "trace": _trace(
                "reflect",
                "replan" if reflection.should_replan else "retry",
                reflection.corrective_action,
            ),
        }
        self._emit(
            "Reflector 决定"
            + ("重新规划" if reflection.should_replan else "局部重试")
        )
        if not reflection.should_replan:
            retry_index = min(
                state["current_step_index"],
                len(plan.steps) - 1,
            )
            retry_step = plan.steps[retry_index].model_copy(
                update={"status": "pending", "result_summary": None}
            )
            updates["plan"] = _replace_step(plan, retry_index, retry_step)
            updates["current_step_index"] = retry_index
        return updates

    def _replan_node(self, state: RepoAgentGraphState) -> dict[str, object]:
        """保留已完成前缀，用失败反馈替换剩余计划。"""

        self._emit("Replanner 正在生成替代步骤")
        plan = state["plan"]
        evaluation = state["evaluation"]
        reflection = state["reflection"]
        if plan is None or evaluation is None or reflection is None:
            reason = "重规划节点缺少计划、评估或反思结果"
            return {
                "status": "failed",
                "stop_reason": reason,
                "trace": _trace("replan", "failed", reason),
            }
        request = ReplanningRequest(
            user_goal=state["user_goal"],
            previous_plan=plan,
            step_results=tuple(state["step_results"]),
            evaluation=evaluation,
            reflection=reflection,
        )
        try:
            replacement = _normalize_plan(self.planner.replan(request))
            prefix_length = min(state["current_step_index"], len(plan.steps))
            preserved = plan.steps[:prefix_length]
            combined = ExecutionPlan(
                rationale=f"{plan.rationale}\n修订：{replacement.rationale}",
                steps=preserved + replacement.steps,
            )
        except Exception as exc:
            reason = f"重规划失败：{type(exc).__name__}: {exc}"
            return {
                "status": "failed",
                "stop_reason": reason,
                "replan_count": state["replan_count"] + 1,
                "trace": _trace("replan", "failed", reason),
            }
        self._emit(f"Replanner 已新增 {len(replacement.steps)} 个步骤")
        return {
            "plan": combined,
            "plan_history": [combined],
            "current_step_index": len(preserved),
            "replan_count": state["replan_count"] + 1,
            "evaluation": None,
            "trace": _trace(
                "replan",
                "created",
                f"保留 {len(preserved)} 步，新增 {len(replacement.steps)} 步",
            ),
        }

    def _report_node(self, state: RepoAgentGraphState) -> dict[str, object]:
        """根据结构化状态确定性生成最终报告。"""

        self._emit("正在根据已验证状态生成最终报告")
        evaluation = state["evaluation"]
        completed = (
            state["status"] != "failed"
            and evaluation is not None
            and evaluation.passed
        )
        status: Literal["completed", "failed"] = (
            "completed" if completed else "failed"
        )
        if completed:
            stop_reason = "任务通过评估"
        elif state["stop_reason"]:
            stop_reason = state["stop_reason"]
        elif evaluation is not None:
            stop_reason = f"任务未通过评估：{evaluation.summary}"
        else:
            stop_reason = "任务在完成评估前停止"

        lines = [
            f"# RepoAgent 运行报告：{state['user_goal']}",
            "",
            f"- 项目：{state['project_id']}",
            f"- 版本：{state['repo_revision']}",
            f"- 状态：{status}",
            f"- 停止原因：{stop_reason}",
            "",
            "## 步骤结果",
        ]
        if state["step_results"]:
            lines.extend(
                f"- {result.step_id} [{result.status}]：{result.summary}"
                for result in state["step_results"]
            )
        else:
            lines.append("- 没有已执行步骤")
        if evaluation is not None:
            lines.extend(["", "## 评估", evaluation.summary])
            if evaluation.evidence:
                lines.extend(
                    ["", "## 证据", *(f"- {item}" for item in evaluation.evidence)]
                )
            if evaluation.issues:
                lines.extend(
                    ["", "## 未解决问题", *(f"- {item}" for item in evaluation.issues)]
                )
        report = "\n".join(lines)
        self._emit(f"任务已结束：{status}")
        return {
            "status": status,
            "stop_reason": stop_reason,
            "final_report": report,
            "trace": _trace("report", status, stop_reason),
        }

    def _route_after_plan(
        self,
        state: RepoAgentGraphState,
    ) -> Literal["execute_step", "report"]:
        """规划失败直接报告，否则进入执行。"""

        return "report" if state["status"] == "failed" else "execute_step"

    def _route_after_execute(
        self,
        state: RepoAgentGraphState,
    ) -> Literal["execute_step", "evaluate"]:
        """步骤失败或全部完成时进入评估。"""

        plan = state["plan"]
        latest = state["step_results"][-1]
        if latest.status == "failed":
            return "evaluate"
        if plan is None or state["current_step_index"] >= len(plan.steps):
            return "evaluate"
        return "execute_step"

    def _route_after_evaluate(
        self,
        state: RepoAgentGraphState,
    ) -> Literal["reflect", "report"]:
        """评估通过或预算耗尽时报告，否则进入反思。"""

        if state["status"] == "failed":
            return "report"
        evaluation = state["evaluation"]
        if evaluation is not None and evaluation.passed:
            return "report"
        if state["reflection_count"] >= self.config.max_reflections:
            return "report"
        return "reflect"

    def _route_after_reflect(
        self,
        state: RepoAgentGraphState,
    ) -> Literal["execute_step", "replan", "report"]:
        """按照反思输出和重规划预算选择下一节点。"""

        if state["status"] == "failed" or state["reflection"] is None:
            return "report"
        if not state["reflection"].should_replan:
            return "execute_step"
        if state["replan_count"] >= self.config.max_replans:
            return "report"
        return "replan"

    def _route_after_replan(
        self,
        state: RepoAgentGraphState,
    ) -> Literal["execute_step", "report"]:
        """重规划成功继续执行，失败则生成报告。"""

        return "report" if state["status"] == "failed" else "execute_step"

    def run(
        self,
        context: ProjectContext,
        user_goal: str,
        *,
        mode: Literal["diagnose", "fix"] = "diagnose",
        run_id: str | None = None,
        thread_id: str | None = None,
        checkpoint_thread_id: str | None = None,
    ) -> RepoAgentRunResult:
        """用显式项目上下文运行主图并返回稳定结果。"""

        if not user_goal.strip():
            raise ValueError("user_goal 不能为空")
        resolved_run_id = run_id or str(uuid4())
        resolved_thread_id = thread_id or resolved_run_id
        if self.checkpointer is not None and checkpoint_thread_id is None:
            raise ValueError("启用 checkpointer 时必须提供物理 checkpoint_thread_id")
        initial_state: RepoAgentGraphState = {
            "run_id": resolved_run_id,
            "thread_id": resolved_thread_id,
            "project_id": context.project_id,
            "repo_root": str(context.repo_root),
            "repo_revision": context.revision,
            "user_goal": user_goal,
            "mode": mode,
            "plan": None,
            "plan_history": [],
            "current_step_index": 0,
            "step_results": [],
            "evaluation": None,
            "evaluation_history": [],
            "reflection": None,
            "reflection_history": [],
            "reflection_count": 0,
            "replan_count": 0,
            "status": "running",
            "stop_reason": "",
            "final_report": "",
            "trace": [],
        }
        invoke_config: dict[str, Any] = {
            "recursion_limit": self.config.recursion_limit
        }
        if checkpoint_thread_id is not None:
            invoke_config["configurable"] = {
                "thread_id": checkpoint_thread_id,
                "checkpoint_ns": "",
            }
        try:
            state = self.graph.invoke(
                initial_state,
                invoke_config,
            )
        except GraphRecursionError as exc:
            reason = f"LangGraph 递归上限触发：{exc}"
            return RepoAgentRunResult(
                run_id=initial_state["run_id"],
                thread_id=initial_state["thread_id"],
                project_id=context.project_id,
                repo_root=str(context.repo_root),
                repo_revision=context.revision,
                user_goal=user_goal,
                mode=mode,
                status="failed",
                plan=None,
                plan_history=(),
                step_results=(),
                evaluation=None,
                evaluation_history=(),
                reflection_history=(),
                reflection_count=0,
                replan_count=0,
                final_report=reason,
                stop_reason=reason,
                trace=(),
            )
        next_nodes: tuple[str, ...] = ()
        if checkpoint_thread_id is not None:
            next_nodes = self.graph.get_state(invoke_config).next
        return self.result_from_state(state, next_nodes=next_nodes)

    def resume_checkpointed(
        self,
        checkpoint_thread_id: str,
    ) -> RepoAgentRunResult:
        """从指定物理线程的最近 checkpoint 继续运行。"""

        if self.checkpointer is None:
            raise ValueError("当前工作流没有启用 checkpointer")
        invoke_config: dict[str, Any] = {
            "recursion_limit": self.config.recursion_limit,
            "configurable": {
                "thread_id": checkpoint_thread_id,
                "checkpoint_ns": "",
            },
        }
        snapshot = self.graph.get_state(invoke_config)
        if not snapshot.values:
            raise LookupError(f"找不到 checkpoint 线程：{checkpoint_thread_id}")
        if not snapshot.next:
            return self.result_from_state(snapshot.values)
        state = self.graph.invoke(None, invoke_config)
        next_nodes = self.graph.get_state(invoke_config).next
        return self.result_from_state(state, next_nodes=next_nodes)

    @staticmethod
    def result_from_state(
        state: dict[str, Any],
        *,
        next_nodes: tuple[str, ...] = (),
    ) -> RepoAgentRunResult:
        """把图内部状态转换为完成、失败或暂停结果。"""

        interrupted = bool(next_nodes)
        status = "interrupted" if interrupted else state["status"]
        stop_reason = (
            f"工作流已暂停，等待节点：{', '.join(next_nodes)}"
            if interrupted
            else state["stop_reason"]
        )
        return RepoAgentRunResult(
            run_id=state["run_id"],
            thread_id=state["thread_id"],
            project_id=state["project_id"],
            repo_root=state["repo_root"],
            repo_revision=state["repo_revision"],
            user_goal=state["user_goal"],
            mode=state["mode"],
            status=status,
            plan=state["plan"],
            plan_history=state["plan_history"],
            step_results=state["step_results"],
            evaluation=state["evaluation"],
            evaluation_history=state["evaluation_history"],
            reflection_history=state["reflection_history"],
            reflection_count=state["reflection_count"],
            replan_count=state["replan_count"],
            final_report=state["final_report"],
            stop_reason=stop_reason,
            trace=state["trace"],
        )
