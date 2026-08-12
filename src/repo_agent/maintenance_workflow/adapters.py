"""Adapters from existing RepoAgent services to maintenance workflow ports."""

from __future__ import annotations

from pathlib import Path

from repo_agent.application import (
    RepoAgentApplication,
    RepoAgentApplicationConfig,
)
from repo_agent.candidate import (
    CandidateEvaluationConfig,
    CandidatePatch,
    CandidatePatchApplier,
    CandidateWorkspace,
    ObjectiveCandidateEvaluator,
    PatchTargetSelection,
    StructuredCandidatePatchGenerator,
)
from repo_agent.llm.contracts import StructuredJSONClient
from repo_agent.projects import ProjectContext
from repo_agent.tools import resolve_python_runtime
from repo_agent.tools.process import ProcessRunner

from .models import PatchEvaluationArtifact, PatchReflection, RepositoryAnalysis
from .ports import PatchProposeRequest


class RepoAgentApplicationAnalyzer:
    """Use the existing read-only Diagnose workflow as repository analysis."""

    def __init__(
        self,
        config: RepoAgentApplicationConfig,
        *,
        structured_client: StructuredJSONClient,
    ) -> None:
        self.config = config
        self.structured_client = structured_client

    def analyze(
        self,
        context: ProjectContext,
        objective: str,
        *,
        thread_id: str | None = None,
    ) -> RepositoryAnalysis:
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
        result = RepoAgentApplication(
            analysis_config,
            structured_client=self.structured_client,
        ).explain(
            "Read-only analysis for maintenance objective: " + objective,
            repo=context.repo_root,
            thread_id=thread_id,
        )
        if result.workflow.status != "completed":
            raise RuntimeError("read-only analysis did not complete")
        evidence = []
        for step in result.workflow.step_results:
            evidence.extend(f"{obs.tool_name}" for obs in step.observations)
        return RepositoryAnalysis(
            run_id=result.workflow.run_id,
            thread_id=result.workflow.thread_id,
            report=result.workflow.final_report,
            evidence=tuple(evidence),
        )


class StructuredPatchTargetSelector:
    """Delegate target selection to the existing structured generator."""

    def __init__(self, generator: StructuredCandidatePatchGenerator) -> None:
        self.generator = generator

    def select(
        self,
        context: ProjectContext,
        objective: str,
        analysis: RepositoryAnalysis,
    ) -> PatchTargetSelection:
        from repo_agent.workflow import RepoAgentRunResult

        synthetic = RepoAgentRunResult(
            run_id=analysis.run_id,
            thread_id=analysis.thread_id,
            project_id=context.project_id,
            repo_root=str(context.repo_root),
            repo_revision=context.revision,
            user_goal=objective,
            mode="fix",
            status="completed",
            plan=None,
            plan_history=(),
            step_results=(),
            evaluation=None,
            evaluation_history=(),
            reflection_history=(),
            reflection_count=0,
            replan_count=0,
            final_report=analysis.report,
            stop_reason="analysis completed",
            trace=(),
        )
        return self.generator.select_targets(context, objective, synthetic)


class StructuredPatchProposer:
    """Delegate patch generation to the existing structured generator."""

    def __init__(self, generator: StructuredCandidatePatchGenerator) -> None:
        self.generator = generator

    def propose(self, request: PatchProposeRequest) -> CandidatePatch:
        return self.generator.generate_patch(
            request.context,
            request.objective,
            request.selection,
        )


class CandidateWorkspacePatchEvaluator:
    """Evaluate a patch in a fresh candidate workspace for every attempt."""

    def __init__(
        self,
        workspace_base: str | Path,
        *,
        allow_code_execution: bool,
        process_runner: ProcessRunner | None = None,
    ) -> None:
        self.workspace_base = Path(workspace_base)
        self.allow_code_execution = allow_code_execution
        self.process_runner = process_runner

    def evaluate(
        self,
        context: ProjectContext,
        patch: CandidatePatch,
        selection: PatchTargetSelection,
        *,
        attempt: int,
    ) -> PatchEvaluationArtifact:
        python_runtime = resolve_python_runtime(context)
        with CandidateWorkspace(
            context,
            self.workspace_base,
            f"{patch.patch_id}-{attempt}",
        ) as workspace:
            application = CandidatePatchApplier(workspace).apply(patch)
            evaluation = ObjectiveCandidateEvaluator(
                workspace,
                CandidateEvaluationConfig(
                    expected_changed_files=tuple(change.path for change in patch.changes),
                    target_tests=selection.target_tests,
                    regression_targets=selection.regression_targets,
                    allow_code_execution=self.allow_code_execution,
                ),
                process_runner=self.process_runner,
                python_runtime=python_runtime,
            ).evaluate_candidate()
        return PatchEvaluationArtifact(
            application=application,
            evaluation=evaluation,
        )


class ObjectivePatchReflector:
    """Reflect from objective validation evidence without rewriting outcomes."""

    def reflect(
        self,
        context: ProjectContext,
        objective: str,
        patch: CandidatePatch,
        artifact: PatchEvaluationArtifact,
        *,
        attempt: int,
    ) -> PatchReflection:
        failing = [
            check
            for check in artifact.evaluation.checks
            if check.status in {"failed", "error"}
        ]
        if not failing:
            return PatchReflection(
                failure_kind="unknown",
                corrective_action="No failing check was available.",
                next_action="stop",
            )
        primary = failing[0]
        evidence = "\n".join(
            part
            for part in [primary.summary, primary.stdout, primary.stderr]
            if part
        )
        if primary.name == "change_scope":
            action = "reselect"
        elif primary.name in {"target_tests", "regression_tests"}:
            action = "repair"
        else:
            action = "stop"
        return PatchReflection(
            failure_kind=primary.name,
            corrective_action=evidence[:5_000],
            next_action=action,
        )
