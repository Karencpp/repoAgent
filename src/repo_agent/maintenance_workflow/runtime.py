"""SQLite checkpoint runtime for the maintenance workflow."""

from __future__ import annotations

from pathlib import Path
import re
import sqlite3
from types import TracebackType
from typing import Callable

from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.sqlite import SqliteSaver

from repo_agent.candidate import (
    AppliedFileChange,
    CandidateEvaluationReport,
    CandidateFileChange,
    CandidatePatch,
    PatchApplicationResult,
    PatchTargetSelection,
    ValidationCheck,
)
from repo_agent.projects import ProjectContext

from .graph import MaintenanceWorkflowConfig, RepoAgentMaintenanceWorkflow
from .models import (
    MaintenanceRunResult,
    MaintenanceTraceEvent,
    PatchEvaluationArtifact,
    PatchReflection,
    RepositoryAnalysis,
)
from .ports import (
    PatchEvaluatorPort,
    PatchPromoterPort,
    PatchProposerPort,
    PatchReflectorPort,
    PatchTargetSelectorPort,
    ProposalStorePort,
    RepositoryAnalyzerPort,
)


THREAD_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")


def build_maintenance_checkpoint_thread_id(
    context: ProjectContext,
    logical_thread_id: str,
) -> str:
    if not THREAD_ID_PATTERN.fullmatch(logical_thread_id):
        raise ValueError("invalid thread_id")
    return f"{context.checkpoint_namespace}:maintenance:{logical_thread_id}"


class SQLiteMaintenanceWorkflowRuntime:
    """Owns SQLite, checkpointer, and compiled maintenance graph."""

    def __init__(
        self,
        database_path: str | Path,
        *,
        context: ProjectContext,
        analyzer: RepositoryAnalyzerPort,
        selector: PatchTargetSelectorPort,
        proposer: PatchProposerPort,
        evaluator: PatchEvaluatorPort,
        reflector: PatchReflectorPort,
        proposal_store: ProposalStorePort,
        promoter: PatchPromoterPort,
        workflow_config: MaintenanceWorkflowConfig | None = None,
        progress_callback: Callable[[str], None] | None = None,
    ) -> None:
        self.database_path = Path(database_path).expanduser().resolve()
        self.context = context
        self.analyzer = analyzer
        self.selector = selector
        self.proposer = proposer
        self.evaluator = evaluator
        self.reflector = reflector
        self.proposal_store = proposal_store
        self.promoter = promoter
        self.workflow_config = workflow_config or MaintenanceWorkflowConfig()
        self.progress_callback = progress_callback
        self._connection: sqlite3.Connection | None = None
        self._checkpointer: SqliteSaver | None = None
        self._workflow: RepoAgentMaintenanceWorkflow | None = None

    def __enter__(self) -> "SQLiteMaintenanceWorkflowRuntime":
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(str(self.database_path), check_same_thread=False)
        serializer = JsonPlusSerializer(
            allowed_msgpack_modules=[
                AppliedFileChange,
                CandidateEvaluationReport,
                CandidateFileChange,
                CandidatePatch,
                MaintenanceTraceEvent,
                PatchApplicationResult,
                PatchEvaluationArtifact,
                PatchReflection,
                PatchTargetSelection,
                RepositoryAnalysis,
                ValidationCheck,
            ]
        )
        checkpointer = SqliteSaver(connection, serde=serializer)
        checkpointer.setup()
        self._connection = connection
        self._checkpointer = checkpointer
        self._workflow = RepoAgentMaintenanceWorkflow(
            self.analyzer,
            self.selector,
            self.proposer,
            self.evaluator,
            self.reflector,
            self.proposal_store,
            self.promoter,
            context=self.context,
            config=self.workflow_config,
            checkpointer=checkpointer,
            progress_callback=self.progress_callback,
        )
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        connection = self._connection
        self._workflow = None
        self._checkpointer = None
        self._connection = None
        if connection is not None:
            connection.close()

    @property
    def workflow(self) -> RepoAgentMaintenanceWorkflow:
        if self._workflow is None:
            raise RuntimeError("maintenance runtime is not open")
        return self._workflow

    def start(
        self,
        objective: str,
        *,
        thread_id: str,
        run_id: str | None = None,
    ) -> MaintenanceRunResult:
        physical = build_maintenance_checkpoint_thread_id(self.context, thread_id)
        return self.workflow.run(
            objective,
            run_id=run_id,
            thread_id=thread_id,
            checkpoint_thread_id=physical,
        )

    def resume(
        self,
        *,
        thread_id: str,
        approved: bool,
    ) -> MaintenanceRunResult:
        physical = build_maintenance_checkpoint_thread_id(self.context, thread_id)
        return self.workflow.resume(
            checkpoint_thread_id=physical,
            approved=approved,
        )
