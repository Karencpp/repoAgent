"""LangGraph PostgreSQL Checkpoint Runtime。"""

from __future__ import annotations

from types import TracebackType
from typing import Callable, Literal

from repo_agent.projects import ProjectContext

from .checkpoints import (
    CheckpointError,
    CheckpointNotFoundError,
    CheckpointThreadExistsError,
    SQLiteWorkflowRuntime,
    _checkpoint_config,
    build_checkpoint_thread_id,
)
from .graph import RepoAgentWorkflow, WorkflowConfig
from .ports import EvaluatorPort, PlannerPort, ReflectorPort, StepExecutorPort


class PostgresWorkflowRuntime:
    """使用 LangGraph PostgreSQL Saver 的 Runtime。"""

    def __init__(
        self,
        dsn: str,
        planner: PlannerPort,
        step_executor: StepExecutorPort,
        evaluator: EvaluatorPort,
        reflector: ReflectorPort,
        *,
        workflow_config: WorkflowConfig | None = None,
        interrupt_before: tuple[str, ...] = (),
        progress_callback: Callable[[str], None] | None = None,
    ) -> None:
        self.dsn = dsn
        self.planner = planner
        self.step_executor = step_executor
        self.evaluator = evaluator
        self.reflector = reflector
        self.workflow_config = workflow_config or WorkflowConfig()
        self.interrupt_before = interrupt_before
        self.progress_callback = progress_callback
        self._manager = None
        self._checkpointer = None
        self._workflow: RepoAgentWorkflow | None = None

    def __enter__(self) -> "PostgresWorkflowRuntime":
        """打开 PostgreSQL Saver 并编译工作流。"""

        try:
            from langgraph.checkpoint.postgres import PostgresSaver
        except ImportError as exc:
            raise CheckpointError(
                "PostgreSQL checkpoint 需要安装可选依赖：repo-agent[postgres]"
            ) from exc
        self._manager = PostgresSaver.from_conn_string(self.dsn)
        self._checkpointer = self._manager.__enter__()
        self._checkpointer.setup()
        self._workflow = RepoAgentWorkflow(
            self.planner,
            self.step_executor,
            self.evaluator,
            self.reflector,
            config=self.workflow_config,
            checkpointer=self._checkpointer,
            interrupt_before=self.interrupt_before,
            progress_callback=self.progress_callback,
        )
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """关闭 PostgreSQL Saver。"""

        self._workflow = None
        self._checkpointer = None
        manager = self._manager
        self._manager = None
        if manager is not None:
            manager.__exit__(exc_type, exc_value, traceback)

    @property
    def workflow(self) -> RepoAgentWorkflow:
        if self._workflow is None:
            raise CheckpointError("PostgresWorkflowRuntime 尚未打开或已经关闭")
        return self._workflow

    @property
    def checkpointer(self):
        if self._checkpointer is None:
            raise CheckpointError("PostgresWorkflowRuntime 尚未打开或已经关闭")
        return self._checkpointer

    def start(
        self,
        context: ProjectContext,
        user_goal: str,
        *,
        thread_id: str,
        run_id: str | None = None,
        mode: Literal["diagnose", "fix"] = "diagnose",
    ):
        physical_thread_id = build_checkpoint_thread_id(context, thread_id)
        existing = self.checkpointer.get_tuple(_checkpoint_config(physical_thread_id))
        if existing is not None:
            raise CheckpointThreadExistsError(
                f"线程已经存在，必须 resume 或换用新 thread_id：{thread_id}"
            )
        return self.workflow.run(
            context,
            user_goal,
            mode=mode,
            run_id=run_id,
            thread_id=thread_id,
            checkpoint_thread_id=physical_thread_id,
        )

    def resume(self, context: ProjectContext, *, thread_id: str):
        physical_thread_id = build_checkpoint_thread_id(context, thread_id)
        snapshot = self.workflow.graph.get_state(_checkpoint_config(physical_thread_id))
        if not snapshot.values:
            raise CheckpointNotFoundError(f"当前项目找不到 checkpoint 线程：{thread_id}")
        SQLiteWorkflowRuntime._validate_snapshot_context(
            snapshot.values,
            context,
            require_same_revision=True,
        )
        return self.workflow.resume_checkpointed(physical_thread_id)
