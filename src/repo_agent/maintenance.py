"""候选修改生成、隔离验证、持久化审批和回写服务。"""

from __future__ import annotations

from contextlib import ExitStack
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from repo_agent.application import (
    RepoAgentApplication,
    RepoAgentApplicationConfig,
    RepoAgentApplicationResult,
)
from repo_agent.candidate import (
    CandidateEvaluationConfig,
    CandidateEvaluationReport,
    CandidatePatch,
    CandidatePatchApplier,
    CandidatePatchPromoter,
    CandidatePromotionResult,
    CandidateWorkspace,
    PatchApplicationResult,
    PatchTargetSelection,
    StructuredCandidatePatchGenerator,
)
from repo_agent.candidate.evaluator import ObjectiveCandidateEvaluator
from repo_agent.llm import structured_client_from_env
from repo_agent.llm.contracts import StructuredJSONClient
from repo_agent.projects import ProjectContextResolver, ProjectRegistry
from repo_agent.tools.process import ProcessRunner
from repo_agent.tools import resolve_python_runtime


class MaintenanceProposalError(RuntimeError):
    """维护候选的创建、读取或批准失败。"""


class MaintenanceProposal(BaseModel):
    """可以跨进程保存并在用户批准后回写的候选制品。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    proposal_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
    project_id: str
    repo_root: str
    repo_revision: str
    objective: str = Field(min_length=1, max_length=20_000)
    analysis_run_id: str
    analysis_thread_id: str
    analysis_report: str
    selection: PatchTargetSelection
    patch: CandidatePatch
    application: PatchApplicationResult
    evaluation: CandidateEvaluationReport
    code_execution_authorized: bool
    created_at: str


def _atomic_json_write(path: Path, payload: dict[str, Any]) -> None:
    """把审批制品原子写入独立状态目录。"""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink(missing_ok=True)


class RepoAgentMaintenanceService:
    """把不确定的模型修改限制在可验证、可审批的候选流程中。"""

    def __init__(
        self,
        config: RepoAgentApplicationConfig | None = None,
        *,
        structured_client: StructuredJSONClient | None = None,
        process_runner: ProcessRunner | None = None,
    ) -> None:
        self.config = config or RepoAgentApplicationConfig()
        self.structured_client = structured_client
        self.process_runner = process_runner

    @property
    def proposals_dir(self) -> Path:
        """返回候选审批制品目录。"""

        return self.config.resolved_state_dir() / "proposals"

    def proposal_path(self, proposal_id: str) -> Path:
        """构造不接受路径片段的候选制品路径。"""

        if not proposal_id or any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-" for char in proposal_id):
            raise MaintenanceProposalError("proposal_id 包含非法字符")
        return self.proposals_dir / f"{proposal_id}.json"

    def save(self, proposal: MaintenanceProposal) -> Path:
        """保存模型补丁、diff 和客观验证报告。"""

        path = self.proposal_path(proposal.proposal_id)
        if path.exists():
            raise MaintenanceProposalError(f"候选制品已经存在：{proposal.proposal_id}")
        _atomic_json_write(path, proposal.model_dump(mode="json"))
        return path

    def load(self, proposal_id: str) -> MaintenanceProposal:
        """从独立状态目录读取并重新校验候选制品。"""

        path = self.proposal_path(proposal_id)
        if not path.is_file():
            raise MaintenanceProposalError(f"找不到候选制品：{proposal_id}")
        try:
            return MaintenanceProposal.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise MaintenanceProposalError(f"候选制品损坏：{proposal_id}") from exc

    def propose(
        self,
        objective: str,
        *,
        repo: str | Path | None = None,
        project: str | None = None,
        allow_code_execution: bool = False,
        thread_id: str | None = None,
    ) -> tuple[MaintenanceProposal, Path, RepoAgentApplicationResult]:
        """只读分析真实仓库，在隔离副本生成并验证候选修改。"""

        if not objective.strip():
            raise ValueError("objective 不能为空")
        with ExitStack() as stack:
            client = self.structured_client
            if client is None:
                owned_client = structured_client_from_env(self.config.llm_provider)
                close_client = getattr(owned_client, "close", None)
                if callable(close_client):
                    stack.callback(close_client)
                client = owned_client

            analysis_config = RepoAgentApplicationConfig(
                state_dir=self.config.state_dir,
                skills_root=self.config.skills_root,
                enable_rag=self.config.enable_rag,
                enable_memory=self.config.enable_memory,
                form_semantic_memory=self.config.form_semantic_memory,
                allow_code_execution=False,
                rag_embedding_dimensions=self.config.rag_embedding_dimensions,
                embedding_provider=self.config.embedding_provider,
                llm_provider=self.config.llm_provider,
                react_config=self.config.react_config,
                workflow_config=self.config.workflow_config,
            )
            analysis = RepoAgentApplication(
                analysis_config,
                structured_client=client,
            ).explain(
                "为后续候选修改做只读分析。维护目标：" + objective,
                repo=repo,
                project=project,
                thread_id=thread_id,
            )
            if analysis.workflow.status != "completed":
                raise MaintenanceProposalError(
                    "只读分析没有通过证据评估，拒绝直接生成补丁"
                )

            generator = StructuredCandidatePatchGenerator(client)
            selection = generator.select_targets(
                analysis.context,
                objective,
                analysis.workflow,
            )
            patch = generator.generate_patch(
                analysis.context,
                objective,
                selection,
            )

            proposal_id = f"proposal-{uuid4().hex[:20]}"
            workspace_base = self.config.resolved_state_dir() / "candidate-workspaces"
            python_runtime = resolve_python_runtime(analysis.context)
            with CandidateWorkspace(
                analysis.context,
                workspace_base,
                proposal_id,
            ) as workspace:
                application = CandidatePatchApplier(workspace).apply(patch)
                evaluation = ObjectiveCandidateEvaluator(
                    workspace,
                    CandidateEvaluationConfig(
                        expected_changed_files=tuple(
                            change.path for change in patch.changes
                        ),
                        target_tests=selection.target_tests,
                        regression_targets=selection.regression_targets,
                        allow_code_execution=allow_code_execution,
                    ),
                    process_runner=self.process_runner,
                    python_runtime=python_runtime,
                ).evaluate_candidate()

            proposal = MaintenanceProposal(
                proposal_id=proposal_id,
                project_id=analysis.context.project_id,
                repo_root=str(analysis.context.repo_root),
                repo_revision=analysis.context.revision,
                objective=objective,
                analysis_run_id=analysis.workflow.run_id,
                analysis_thread_id=analysis.workflow.thread_id,
                analysis_report=analysis.workflow.final_report,
                selection=selection,
                patch=patch,
                application=application,
                evaluation=evaluation,
                code_execution_authorized=allow_code_execution,
                created_at=datetime.now(timezone.utc).isoformat(),
            )
            return proposal, self.save(proposal), analysis

    def apply(
        self,
        proposal_id: str,
        *,
        approved: bool,
    ) -> CandidatePromotionResult:
        """加载候选制品，经显式批准后回写原仓库。"""

        proposal = self.load(proposal_id)
        registry = ProjectRegistry(self.config.resolved_state_dir() / "projects.json")
        context = ProjectContextResolver(registry).resolve(repo=proposal.repo_root)
        if context.project_id != proposal.project_id:
            raise MaintenanceProposalError("候选制品与当前项目身份不一致")
        if context.revision != proposal.repo_revision:
            raise MaintenanceProposalError("目标仓库已变化，请重新生成候选")
        result = CandidatePatchPromoter().promote(
            context,
            proposal.proposal_id,
            proposal.patch,
            proposal.evaluation,
            approved=approved,
        )
        audit_path = self.proposals_dir / f"{proposal_id}.applied.json"
        _atomic_json_write(audit_path, result.model_dump(mode="json"))
        return result

    def start_workflow(
        self,
        objective: str,
        *,
        repo: str | Path | None = None,
        project: str | None = None,
        allow_code_execution: bool = False,
        thread_id: str | None = None,
    ):
        """Run the LangGraph maintenance workflow until approval is required."""

        if not objective.strip():
            raise ValueError("objective 不能为空")
        from repo_agent.maintenance_workflow.adapters import (
            CandidateWorkspacePatchEvaluator,
            ObjectivePatchReflector,
            RepoAgentApplicationAnalyzer,
            StructuredPatchProposer,
            StructuredPatchTargetSelector,
        )
        from repo_agent.maintenance_workflow.runtime import (
            SQLiteMaintenanceWorkflowRuntime,
        )

        state_dir = self.config.resolved_state_dir()
        state_dir.mkdir(parents=True, exist_ok=True)
        context = ProjectContextResolver(
            ProjectRegistry(state_dir / "projects.json")
        ).resolve(repo=repo, project=project)
        with ExitStack() as stack:
            client = self.structured_client
            if client is None:
                owned_client = structured_client_from_env(self.config.llm_provider)
                close_client = getattr(owned_client, "close", None)
                if callable(close_client):
                    stack.callback(close_client)
                client = owned_client
            generator = StructuredCandidatePatchGenerator(client)
            runtime = SQLiteMaintenanceWorkflowRuntime(
                state_dir / "maintenance-checkpoints.sqlite3",
                context=context,
                analyzer=RepoAgentApplicationAnalyzer(
                    self.config,
                    structured_client=client,
                ),
                selector=StructuredPatchTargetSelector(generator),
                proposer=StructuredPatchProposer(generator),
                evaluator=CandidateWorkspacePatchEvaluator(
                    state_dir / "candidate-workspaces",
                    allow_code_execution=allow_code_execution,
                    process_runner=self.process_runner,
                ),
                reflector=ObjectivePatchReflector(),
                proposal_store=_MaintenanceWorkflowProposalStore(self),
                promoter=_MaintenanceWorkflowPromoter(self),
            )
            with runtime:
                return runtime.start(
                    objective,
                    thread_id=thread_id or f"fix-{uuid4().hex[:20]}",
                )

    def resume_workflow(
        self,
        *,
        thread_id: str,
        repo: str | Path | None = None,
        project: str | None = None,
        approved: bool,
    ):
        """Resume a checkpointed maintenance workflow with an explicit decision."""

        from repo_agent.maintenance_workflow.adapters import (
            CandidateWorkspacePatchEvaluator,
            ObjectivePatchReflector,
            RepoAgentApplicationAnalyzer,
            StructuredPatchProposer,
            StructuredPatchTargetSelector,
        )
        from repo_agent.maintenance_workflow.runtime import (
            SQLiteMaintenanceWorkflowRuntime,
        )

        state_dir = self.config.resolved_state_dir()
        context = ProjectContextResolver(
            ProjectRegistry(state_dir / "projects.json")
        ).resolve(repo=repo, project=project)
        with ExitStack() as stack:
            client = self.structured_client
            if client is None:
                owned_client = structured_client_from_env(self.config.llm_provider)
                close_client = getattr(owned_client, "close", None)
                if callable(close_client):
                    stack.callback(close_client)
                client = owned_client
            generator = StructuredCandidatePatchGenerator(client)
            runtime = SQLiteMaintenanceWorkflowRuntime(
                state_dir / "maintenance-checkpoints.sqlite3",
                context=context,
                analyzer=RepoAgentApplicationAnalyzer(
                    self.config,
                    structured_client=client,
                ),
                selector=StructuredPatchTargetSelector(generator),
                proposer=StructuredPatchProposer(generator),
                evaluator=CandidateWorkspacePatchEvaluator(
                    state_dir / "candidate-workspaces",
                    allow_code_execution=True,
                    process_runner=self.process_runner,
                ),
                reflector=ObjectivePatchReflector(),
                proposal_store=_MaintenanceWorkflowProposalStore(self),
                promoter=_MaintenanceWorkflowPromoter(self),
            )
            with runtime:
                return runtime.resume(thread_id=thread_id, approved=approved)


class _MaintenanceWorkflowProposalStore:
    """Persist maintenance workflow results using the existing proposal format."""

    def __init__(self, service: RepoAgentMaintenanceService) -> None:
        self.service = service

    def save(self, result) -> tuple[str, str]:
        if (
            result.analysis is None
            or result.selected_targets is None
            or result.patch is None
            or result.evaluation is None
        ):
            raise MaintenanceProposalError("workflow result is incomplete")
        proposal_id = f"proposal-{uuid4().hex[:20]}"
        from repo_agent.candidate import PatchApplicationResult

        application = PatchApplicationResult(
            patch_id=result.patch.patch_id,
            summary=result.patch.summary,
            changed_files=result.evaluation.changed_files,
            changes=(),
            unified_diff=result.evaluation.unified_diff,
        )
        proposal = MaintenanceProposal(
            proposal_id=proposal_id,
            project_id=result.project_id,
            repo_root=result.repo_root,
            repo_revision=result.repo_revision,
            objective=result.objective,
            analysis_run_id=result.analysis.run_id,
            analysis_thread_id=result.analysis.thread_id,
            analysis_report=result.analysis.report,
            selection=result.selected_targets,
            patch=result.patch,
            application=application,
            evaluation=result.evaluation,
            code_execution_authorized=True,
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        path = self.service.save(proposal)
        return proposal_id, str(path)


class _MaintenanceWorkflowPromoter:
    """Promote an already persisted proposal through the existing apply path."""

    def __init__(self, service: RepoAgentMaintenanceService) -> None:
        self.service = service

    def promote(self, context, proposal_id: str, *, approved: bool):
        return self.service.apply(proposal_id, approved=approved)
