"""Ports used by the maintenance graph."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from repo_agent.candidate import (
    CandidatePatch,
    CandidatePromotionResult,
    PatchTargetSelection,
)
from repo_agent.projects import ProjectContext

from .models import (
    MaintenanceRunResult,
    PatchEvaluationArtifact,
    PatchReflection,
    RepositoryAnalysis,
)


@dataclass(frozen=True, slots=True)
class PatchProposeRequest:
    """Inputs for generating or repairing one candidate patch."""

    context: ProjectContext
    objective: str
    analysis: RepositoryAnalysis
    selection: PatchTargetSelection
    patch_history: tuple[CandidatePatch, ...]
    evaluation_history: tuple[PatchEvaluationArtifact, ...]
    reflection: PatchReflection | None
    attempt: int


class RepositoryAnalyzerPort(Protocol):
    def analyze(
        self,
        context: ProjectContext,
        objective: str,
        *,
        thread_id: str | None = None,
    ) -> RepositoryAnalysis:
        """Return read-only repository analysis."""


class PatchTargetSelectorPort(Protocol):
    def select(
        self,
        context: ProjectContext,
        objective: str,
        analysis: RepositoryAnalysis,
    ) -> PatchTargetSelection:
        """Select allowed files and test targets."""


class PatchProposerPort(Protocol):
    def propose(self, request: PatchProposeRequest) -> CandidatePatch:
        """Generate a bounded candidate patch."""


class PatchEvaluatorPort(Protocol):
    def evaluate(
        self,
        context: ProjectContext,
        patch: CandidatePatch,
        selection: PatchTargetSelection,
        *,
        attempt: int,
    ) -> PatchEvaluationArtifact:
        """Apply and objectively evaluate a candidate patch."""


class PatchReflectorPort(Protocol):
    def reflect(
        self,
        context: ProjectContext,
        objective: str,
        patch: CandidatePatch,
        artifact: PatchEvaluationArtifact,
        *,
        attempt: int,
    ) -> PatchReflection:
        """Reflect on objective evaluation evidence."""


class ProposalStorePort(Protocol):
    def save(self, result: MaintenanceRunResult) -> tuple[str, str]:
        """Persist the passed candidate and return proposal id and path."""


class PatchPromoterPort(Protocol):
    def promote(
        self,
        context: ProjectContext,
        proposal_id: str,
        *,
        approved: bool,
    ) -> CandidatePromotionResult:
        """Promote an approved proposal into the real repository."""
