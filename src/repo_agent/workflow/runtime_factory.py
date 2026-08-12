"""Checkpoint Runtime 统一工厂。"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from repo_agent.storage import StorageConfig

from .checkpoints import SQLiteWorkflowRuntime
from .graph import WorkflowConfig
from .ports import EvaluatorPort, PlannerPort, ReflectorPort, StepExecutorPort


class CheckpointRuntimeFactory:
    """按存储配置创建 DiagnoseGraph 使用的 Checkpoint Runtime。"""

    def __init__(self, storage: StorageConfig) -> None:
        self.storage = storage

    def create(
        self,
        *,
        planner: PlannerPort,
        step_executor: StepExecutorPort,
        evaluator: EvaluatorPort,
        reflector: ReflectorPort,
        workflow_config: WorkflowConfig | None = None,
        interrupt_before: tuple[str, ...] = (),
        progress_callback: Callable[[str], None] | None = None,
    ):
        """创建当前配置对应的 Runtime。"""

        if self.storage.backend == "sqlite":
            state_dir = Path(self.storage.sqlite_state_dir or ".repo-agent").resolve()
            return SQLiteWorkflowRuntime(
                state_dir / "checkpoints.sqlite3",
                planner,
                step_executor,
                evaluator,
                reflector,
                workflow_config=workflow_config,
                interrupt_before=interrupt_before,
                progress_callback=progress_callback,
            )
        from .runtime_postgres import PostgresWorkflowRuntime

        return PostgresWorkflowRuntime(
            self.storage.require_postgres_dsn(),
            planner,
            step_executor,
            evaluator,
            reflector,
            workflow_config=workflow_config,
            interrupt_before=interrupt_before,
            progress_callback=progress_callback,
        )
