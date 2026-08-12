"""Structured state models for the maintenance graph."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from repo_agent.candidate import (
    CandidateEvaluationReport,
    CandidatePatch,
    CandidatePromotionResult,
    PatchApplicationResult,
    PatchTargetSelection,
)


class MaintenanceModel(BaseModel):
    """Common strict settings for maintenance workflow models."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class RepositoryAnalysis(MaintenanceModel):
    """Read-only analysis used to constrain patch generation."""

    run_id: str
    thread_id: str
    report: str = Field(min_length=1, max_length=200_000)
    evidence: tuple[str, ...] = ()
    relevant_files: tuple[str, ...] = ()
    risks: tuple[str, ...] = ()
    suggested_tests: tuple[str, ...] = ()


class PatchEvaluationArtifact(MaintenanceModel):
    """Patch application and objective evaluation for one attempt."""

    application: PatchApplicationResult
    evaluation: CandidateEvaluationReport


class PatchReflection(MaintenanceModel):
    """Structured failure reflection that drives repair or stop routing."""

    failure_kind: str = Field(min_length=1, max_length=200)
    corrective_action: str = Field(min_length=1, max_length=5_000)
    next_action: Literal["repair", "reselect", "stop"]


class MaintenanceTraceEvent(MaintenanceModel):
    """Compact graph trace event."""

    node: str
    event: str
    summary: str


class MaintenanceRunResult(MaintenanceModel):
    """Stable result returned by the maintenance workflow runtime."""

    run_id: str
    thread_id: str
    project_id: str
    repo_root: str
    repo_revision: str
    objective: str
    status: Literal[
        "running",
        "waiting_approval",
        "completed",
        "failed",
        "interrupted",
    ]
    stop_reason: str
    analysis: RepositoryAnalysis | None
    selected_targets: PatchTargetSelection | None
    patch: CandidatePatch | None
    patch_history: tuple[CandidatePatch, ...]
    patch_attempt: int
    evaluation: CandidateEvaluationReport | None
    evaluation_history: tuple[CandidateEvaluationReport, ...]
    reflection: PatchReflection | None
    reflection_history: tuple[PatchReflection, ...]
    proposal_id: str | None
    approval_status: Literal["pending", "approved", "rejected"] | None
    promotion_result: CandidatePromotionResult | None
    final_report: str
    trace: tuple[MaintenanceTraceEvent, ...]
