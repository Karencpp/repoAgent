"""按项目隔离的 SQLite Checkpoint 生命周期与恢复入口。"""

from __future__ import annotations

from pathlib import Path
import re
import sqlite3
from types import TracebackType
from typing import Callable, Literal

from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.sqlite import SqliteSaver
from pydantic import BaseModel, ConfigDict, Field

from repo_agent.projects import ProjectContext

from .graph import RepoAgentWorkflow, WorkflowConfig
from .models import (
    EvaluationResult,
    ExecutionPlan,
    GraphTraceEvent,
    PlanStep,
    ReflectionResult,
    RepoAgentRunResult,
    StepExecution,
    StepToolObservation,
)
from .ports import EvaluatorPort, PlannerPort, ReflectorPort, StepExecutorPort


THREAD_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")


class CheckpointError(RuntimeError):
    """Checkpoint 生命周期错误的公共基类。"""


class InvalidCheckpointThreadError(CheckpointError):
    """逻辑 thread id 不满足稳定标识约束。"""


class CheckpointThreadExistsError(CheckpointError):
    """尝试用已有线程启动一份全新状态。"""


class CheckpointNotFoundError(CheckpointError):
    """当前项目命名空间内找不到指定线程。"""


class CheckpointProjectMismatchError(CheckpointError):
    """Checkpoint 内的项目身份与当前上下文不一致。"""


class CheckpointRevisionMismatchError(CheckpointError):
    """目标仓库版本已经不同，不能静默恢复旧观察。"""


class CheckpointStoreClosedError(CheckpointError):
    """在 SQLite Runtime 打开前或关闭后访问持久化能力。"""


class CheckpointSnapshotSummary(BaseModel):
    """供状态查询和面试 Trace 使用的 checkpoint 摘要。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    checkpoint_id: str
    thread_id: str
    run_id: str
    project_id: str
    repo_revision: str
    status: Literal["running", "completed", "failed"]
    is_terminal: bool
    next_nodes: tuple[str, ...]
    step: int
    created_at: str | None


def _validate_logical_thread_id(thread_id: str) -> str:
    """限制 thread id，避免空值、路径和不可审计字符。"""

    if not THREAD_ID_PATTERN.fullmatch(thread_id):
        raise InvalidCheckpointThreadError(
            "thread_id 必须以字母或数字开头，只能包含字母、数字、点、下划线和短横线，且不超过 100 个字符"
        )
    return thread_id


def build_checkpoint_thread_id(
    context: ProjectContext,
    logical_thread_id: str,
) -> str:
    """把用户可见 thread id 转换成带项目命名空间的物理键。"""

    validated = _validate_logical_thread_id(logical_thread_id)
    return f"{context.checkpoint_namespace}:{validated}"


def _checkpoint_config(physical_thread_id: str) -> dict[str, object]:
    """构造根图使用的 LangGraph checkpoint 配置。"""

    return {
        "configurable": {
            "thread_id": physical_thread_id,
            "checkpoint_ns": "",
        }
    }


class SQLiteWorkflowRuntime:
    """管理 SQLite 连接、Checkpointer、工作流和恢复校验。"""

    def __init__(
        self,
        database_path: str | Path,
        planner: PlannerPort,
        step_executor: StepExecutorPort,
        evaluator: EvaluatorPort,
        reflector: ReflectorPort,
        *,
        workflow_config: WorkflowConfig | None = None,
        interrupt_before: tuple[str, ...] = (),
        progress_callback: Callable[[str], None] | None = None,
    ) -> None:
        self.database_path = Path(database_path).expanduser().resolve()
        self.planner = planner
        self.step_executor = step_executor
        self.evaluator = evaluator
        self.reflector = reflector
        self.workflow_config = workflow_config or WorkflowConfig()
        self.interrupt_before = interrupt_before
        self.progress_callback = progress_callback
        self._connection: sqlite3.Connection | None = None
        self._checkpointer: SqliteSaver | None = None
        self._workflow: RepoAgentWorkflow | None = None

    def __enter__(self) -> "SQLiteWorkflowRuntime":
        """打开 SQLite 并编译绑定 Checkpointer 的工作流。"""

        if self._connection is not None:
            raise CheckpointError("SQLiteWorkflowRuntime 不能重复打开")
        if self.database_path.exists() and self.database_path.is_dir():
            raise CheckpointError(f"checkpoint 数据库路径是目录：{self.database_path}")
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(str(self.database_path), check_same_thread=False)
        try:
            serializer = JsonPlusSerializer(
                allowed_msgpack_modules=[
                    EvaluationResult,
                    ExecutionPlan,
                    GraphTraceEvent,
                    PlanStep,
                    ReflectionResult,
                    StepExecution,
                    StepToolObservation,
                ]
            )
            checkpointer = SqliteSaver(connection, serde=serializer)
            checkpointer.setup()
        except Exception:
            connection.close()
            raise
        self._connection = connection
        self._checkpointer = checkpointer
        self._workflow = RepoAgentWorkflow(
            self.planner,
            self.step_executor,
            self.evaluator,
            self.reflector,
            config=self.workflow_config,
            checkpointer=checkpointer,
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
        """关闭连接并使 Runtime 失效。"""

        connection = self._connection
        self._workflow = None
        self._checkpointer = None
        self._connection = None
        if connection is not None:
            connection.close()

    @property
    def workflow(self) -> RepoAgentWorkflow:
        """返回已经绑定 SQLite Checkpointer 的工作流。"""

        if self._workflow is None:
            raise CheckpointStoreClosedError("SQLiteWorkflowRuntime 尚未打开或已经关闭")
        return self._workflow

    @property
    def checkpointer(self) -> SqliteSaver:
        """返回当前打开的 SQLite Saver。"""

        if self._checkpointer is None:
            raise CheckpointStoreClosedError("SQLiteWorkflowRuntime 尚未打开或已经关闭")
        return self._checkpointer

    def _latest_raw_snapshot(
        self,
        context: ProjectContext,
        thread_id: str,
    ):
        """读取当前项目物理线程的最新原始快照。"""

        physical_thread_id = build_checkpoint_thread_id(context, thread_id)
        return self.workflow.graph.get_state(
            _checkpoint_config(physical_thread_id)
        )

    @staticmethod
    def _validate_snapshot_context(
        snapshot_values: dict[str, object],
        context: ProjectContext,
        *,
        require_same_revision: bool = False,
    ) -> None:
        """恢复前再次验证项目身份、路径和仓库版本。"""

        if snapshot_values.get("project_id") != context.project_id:
            raise CheckpointProjectMismatchError("checkpoint 的 project_id 与当前项目不一致")
        if snapshot_values.get("repo_root") != str(context.repo_root):
            raise CheckpointProjectMismatchError("checkpoint 的 repo_root 与当前项目不一致")
        saved_revision = snapshot_values.get("repo_revision")
        if require_same_revision and saved_revision != context.revision:
            raise CheckpointRevisionMismatchError(
                f"checkpoint 版本为 {saved_revision}，当前仓库版本为 {context.revision}"
            )

    def start(
        self,
        context: ProjectContext,
        user_goal: str,
        *,
        thread_id: str,
        run_id: str | None = None,
        mode: Literal["diagnose", "fix"] = "diagnose",
    ) -> RepoAgentRunResult:
        """在新的项目级线程中启动工作流，拒绝覆盖已有状态。"""

        physical_thread_id = build_checkpoint_thread_id(context, thread_id)
        existing = self.checkpointer.get_tuple(
            _checkpoint_config(physical_thread_id)
        )
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

    def resume(
        self,
        context: ProjectContext,
        *,
        thread_id: str,
    ) -> RepoAgentRunResult:
        """校验项目与 revision 后从最近状态边界继续。"""

        physical_thread_id = build_checkpoint_thread_id(context, thread_id)
        snapshot = self._latest_raw_snapshot(context, thread_id)
        if not snapshot.values:
            raise CheckpointNotFoundError(
                f"当前项目找不到 checkpoint 线程：{thread_id}"
            )
        self._validate_snapshot_context(
            snapshot.values,
            context,
            require_same_revision=True,
        )
        return self.workflow.resume_checkpointed(physical_thread_id)

    def latest(
        self,
        context: ProjectContext,
        *,
        thread_id: str,
    ) -> CheckpointSnapshotSummary:
        """返回指定线程的最新 checkpoint 摘要。"""

        snapshot = self._latest_raw_snapshot(context, thread_id)
        if not snapshot.values:
            raise CheckpointNotFoundError(
                f"当前项目找不到 checkpoint 线程：{thread_id}"
            )
        self._validate_snapshot_context(snapshot.values, context)
        return self._snapshot_summary(snapshot)

    def history(
        self,
        context: ProjectContext,
        *,
        thread_id: str,
        limit: int = 50,
    ) -> tuple[CheckpointSnapshotSummary, ...]:
        """按新到旧返回有限 checkpoint 历史。"""

        if limit < 1 or limit > 500:
            raise ValueError("history limit 必须在 1 到 500 之间")
        physical_thread_id = build_checkpoint_thread_id(context, thread_id)
        snapshots = list(
            self.workflow.graph.get_state_history(
                _checkpoint_config(physical_thread_id)
            )
        )[:limit]
        if not snapshots:
            raise CheckpointNotFoundError(
                f"当前项目找不到 checkpoint 线程：{thread_id}"
            )
        self._validate_snapshot_context(snapshots[0].values, context)
        return tuple(self._snapshot_summary(snapshot) for snapshot in snapshots)

    def delete(
        self,
        context: ProjectContext,
        *,
        thread_id: str,
    ) -> None:
        """显式删除当前项目下一个逻辑线程的全部 checkpoint。"""

        physical_thread_id = build_checkpoint_thread_id(context, thread_id)
        snapshot = self._latest_raw_snapshot(context, thread_id)
        if not snapshot.values:
            raise CheckpointNotFoundError(
                f"当前项目找不到 checkpoint 线程：{thread_id}"
            )
        self._validate_snapshot_context(snapshot.values, context)
        self.checkpointer.delete_thread(physical_thread_id)

    @staticmethod
    def _snapshot_summary(snapshot) -> CheckpointSnapshotSummary:
        """把 LangGraph StateSnapshot 转换成稳定摘要。"""

        values = snapshot.values
        next_nodes = tuple(snapshot.next)
        status = values.get("status", "running")
        configurable = snapshot.config.get("configurable", {})
        return CheckpointSnapshotSummary(
            checkpoint_id=str(configurable.get("checkpoint_id", "")),
            thread_id=str(values.get("thread_id", "")),
            run_id=str(values.get("run_id", "")),
            project_id=str(values.get("project_id", "")),
            repo_revision=str(values.get("repo_revision", "")),
            status=status,
            is_terminal=not next_nodes,
            next_nodes=next_nodes,
            step=int(snapshot.metadata.get("step", 0)),
            created_at=snapshot.created_at,
        )
