"""LangGraph implementation of the maintenance patch loop."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import operator
from typing import Annotated, Any, Callable, Literal, TypedDict
from uuid import uuid4

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from repo_agent.candidate import CandidatePatch, PatchTargetSelection
from repo_agent.projects import ProjectContext

from .models import (
    MaintenanceRunResult,
    MaintenanceTraceEvent,
    PatchEvaluationArtifact,
    PatchReflection,
    RepositoryAnalysis,
)
from .ports import (
    PatchEvaluatorPort,
    PatchProposeRequest,
    PatchPromoterPort,
    PatchProposerPort,
    PatchReflectorPort,
    PatchTargetSelectorPort,
    ProposalStorePort,
    RepositoryAnalyzerPort,
)


class MaintenanceGraphState(TypedDict):
    run_id: str
    thread_id: str
    project_id: str
    repo_root: str
    repo_revision: str
    objective: str
    analysis: RepositoryAnalysis | None
    selected_targets: PatchTargetSelection | None
    patch: CandidatePatch | None
    patch_history: Annotated[list[CandidatePatch], operator.add]
    patch_fingerprints: Annotated[list[str], operator.add]
    patch_attempt: int
    evaluation_artifact: PatchEvaluationArtifact | None
    evaluation_artifact_history: Annotated[list[PatchEvaluationArtifact], operator.add]
    reflection: PatchReflection | None
    reflection_history: Annotated[list[PatchReflection], operator.add]
    proposal_id: str | None
    proposal_path: str | None
    approval_status: Literal["pending", "approved", "rejected"] | None
    promotion_result: Any | None
    status: Literal["running", "waiting_approval", "completed", "failed"]
    stop_reason: str
    final_report: str
    trace: Annotated[list[MaintenanceTraceEvent], operator.add]


@dataclass(frozen=True, slots=True)
class MaintenanceWorkflowConfig:
    """Retry and recursion limits for the maintenance graph."""

    max_patch_attempts: int = 3
    recursion_limit: int = 50

    def __post_init__(self) -> None:
        if self.max_patch_attempts < 1:
            raise ValueError("max_patch_attempts must be at least 1")
        if self.recursion_limit < 10:
            raise ValueError("recursion_limit must be at least 10")


def _trace(node: str, event: str, summary: str) -> list[MaintenanceTraceEvent]:
    return [MaintenanceTraceEvent(node=node, event=event, summary=summary)]


def _patch_fingerprint(patch: CandidatePatch) -> str:
    payload = patch.model_dump_json()
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class RepoAgentMaintenanceWorkflow:
    """Patch generation, evaluation, reflection, approval, and promotion graph."""

    def __init__(
        self,
        analyzer: RepositoryAnalyzerPort,
        selector: PatchTargetSelectorPort,
        proposer: PatchProposerPort,
        evaluator: PatchEvaluatorPort,
        reflector: PatchReflectorPort,
        proposal_store: ProposalStorePort,
        promoter: PatchPromoterPort,
        *,
        context: ProjectContext,
        config: MaintenanceWorkflowConfig | None = None,
        checkpointer: BaseCheckpointSaver[Any] | None = None,
        progress_callback: Callable[[str], None] | None = None,
    ) -> None:
        self.analyzer = analyzer
        self.selector = selector
        self.proposer = proposer
        self.evaluator = evaluator
        self.reflector = reflector
        self.proposal_store = proposal_store
        self.promoter = promoter
        self.context = context
        self.config = config or MaintenanceWorkflowConfig()
        self.checkpointer = checkpointer
        self.progress_callback = progress_callback
        self.graph = self._build_graph()

    def _emit(self, message: str) -> None:
        if self.progress_callback is not None:
            self.progress_callback(message)

    def _build_graph(self):
        builder = StateGraph(MaintenanceGraphState)
        builder.add_node("analyze_repository", self._analyze_repository)
        builder.add_node("select_targets", self._select_targets)
        builder.add_node("propose_patch", self._propose_patch)
        builder.add_node("evaluate_patch", self._evaluate_patch)
        builder.add_node("reflect_patch", self._reflect_patch)
        builder.add_node("persist_proposal", self._persist_proposal)
        builder.add_node("await_approval", self._await_approval)
        builder.add_node("promote_patch", self._promote_patch)
        builder.add_node("report_success", self._report_success)
        builder.add_node("report_failure", self._report_failure)
        builder.add_node("report_rejected", self._report_rejected)

        builder.add_edge(START, "analyze_repository")
        builder.add_edge("analyze_repository", "select_targets")
        builder.add_edge("select_targets", "propose_patch")
        builder.add_conditional_edges(
            "propose_patch",
            self._route_after_propose,
            {
                "evaluate_patch": "evaluate_patch",
                "report_failure": "report_failure",
            },
        )
        builder.add_conditional_edges(
            "evaluate_patch",
            self._route_after_evaluate,
            {
                "persist_proposal": "persist_proposal",
                "reflect_patch": "reflect_patch",
                "report_failure": "report_failure",
            },
        )
        builder.add_conditional_edges(
            "reflect_patch",
            self._route_after_reflect,
            {
                "propose_patch": "propose_patch",
                "select_targets": "select_targets",
                "report_failure": "report_failure",
            },
        )
        builder.add_edge("persist_proposal", "await_approval")
        builder.add_conditional_edges(
            "await_approval",
            self._route_after_approval,
            {
                "promote_patch": "promote_patch",
                "report_rejected": "report_rejected",
            },
        )
        builder.add_edge("promote_patch", "report_success")
        builder.add_edge("report_success", END)
        builder.add_edge("report_failure", END)
        builder.add_edge("report_rejected", END)
        return builder.compile(
            checkpointer=self.checkpointer,
            name="repo-agent-maintenance-workflow",
        )

    def _analyze_repository(self, state: MaintenanceGraphState) -> dict[str, object]:
        self._emit("Maintenance analyze_repository")
        try:
            analysis = self.analyzer.analyze(
                self.context,
                state["objective"],
                thread_id=state["thread_id"],
            )
            return {
                "analysis": analysis,
                "trace": _trace("analyze_repository", "completed", analysis.report[:500]),
            }
        except Exception as exc:
            reason = f"analyze_repository failed: {type(exc).__name__}: {exc}"
            return {
                "status": "failed",
                "stop_reason": reason,
                "trace": _trace("analyze_repository", "failed", reason),
            }

    def _select_targets(self, state: MaintenanceGraphState) -> dict[str, object]:
        self._emit("Maintenance select_targets")
        analysis = state["analysis"]
        if analysis is None:
            return {"status": "failed", "stop_reason": "missing analysis"}
        try:
            selection = self.selector.select(self.context, state["objective"], analysis)
            return {
                "selected_targets": selection,
                "trace": _trace("select_targets", "completed", ",".join(selection.paths)),
            }
        except Exception as exc:
            reason = f"select_targets failed: {type(exc).__name__}: {exc}"
            return {
                "status": "failed",
                "stop_reason": reason,
                "trace": _trace("select_targets", "failed", reason),
            }

    def _propose_patch(self, state: MaintenanceGraphState) -> dict[str, object]:
        self._emit("Maintenance propose_patch")
        analysis = state["analysis"]
        selection = state["selected_targets"]
        if analysis is None or selection is None:
            return {"status": "failed", "stop_reason": "missing analysis or selection"}
        attempt = state["patch_attempt"] + 1
        try:
            patch = self.proposer.propose(
                PatchProposeRequest(
                    context=self.context,
                    objective=state["objective"],
                    analysis=analysis,
                    selection=selection,
                    patch_history=tuple(state["patch_history"]),
                    evaluation_history=tuple(state["evaluation_artifact_history"]),
                    reflection=state["reflection"],
                    attempt=attempt,
                )
            )
            fingerprint = _patch_fingerprint(patch)
            if fingerprint in set(state["patch_fingerprints"]):
                return {
                    "status": "failed",
                    "stop_reason": "duplicate patch fingerprint",
                    "trace": _trace("propose_patch", "duplicate", patch.patch_id),
                }
            return {
                "patch": patch,
                "patch_history": [patch],
                "patch_fingerprints": [fingerprint],
                "patch_attempt": attempt,
                "trace": _trace("propose_patch", "completed", patch.summary),
            }
        except Exception as exc:
            reason = f"propose_patch failed: {type(exc).__name__}: {exc}"
            return {
                "status": "failed",
                "stop_reason": reason,
                "trace": _trace("propose_patch", "failed", reason),
            }

    def _evaluate_patch(self, state: MaintenanceGraphState) -> dict[str, object]:
        self._emit("Maintenance evaluate_patch")
        patch = state["patch"]
        selection = state["selected_targets"]
        if patch is None or selection is None:
            return {"status": "failed", "stop_reason": "missing patch or selection"}
        try:
            artifact = self.evaluator.evaluate(
                self.context,
                patch,
                selection,
                attempt=state["patch_attempt"],
            )
            return {
                "evaluation_artifact": artifact,
                "evaluation_artifact_history": [artifact],
                "trace": _trace("evaluate_patch", "passed" if artifact.evaluation.passed else "failed", artifact.evaluation.summary),
            }
        except Exception as exc:
            reason = f"evaluate_patch failed: {type(exc).__name__}: {exc}"
            return {
                "status": "failed",
                "stop_reason": reason,
                "trace": _trace("evaluate_patch", "error", reason),
            }

    def _reflect_patch(self, state: MaintenanceGraphState) -> dict[str, object]:
        self._emit("Maintenance reflect_patch")
        patch = state["patch"]
        artifact = state["evaluation_artifact"]
        if patch is None or artifact is None:
            return {"status": "failed", "stop_reason": "missing patch evaluation"}
        try:
            reflection = self.reflector.reflect(
                self.context,
                state["objective"],
                patch,
                artifact,
                attempt=state["patch_attempt"],
            )
            return {
                "reflection": reflection,
                "reflection_history": [reflection],
                "trace": _trace("reflect_patch", reflection.next_action, reflection.corrective_action),
            }
        except Exception as exc:
            reason = f"reflect_patch failed: {type(exc).__name__}: {exc}"
            return {
                "status": "failed",
                "stop_reason": reason,
                "trace": _trace("reflect_patch", "failed", reason),
            }

    def _persist_proposal(self, state: MaintenanceGraphState) -> dict[str, object]:
        self._emit("Maintenance persist_proposal")
        result = self.result_from_state(state)
        try:
            proposal_id, proposal_path = self.proposal_store.save(result)
            return {
                "proposal_id": proposal_id,
                "proposal_path": proposal_path,
                "status": "waiting_approval",
                "approval_status": "pending",
                "trace": _trace("persist_proposal", "completed", proposal_id),
            }
        except Exception as exc:
            reason = f"persist_proposal failed: {type(exc).__name__}: {exc}"
            return {
                "status": "failed",
                "stop_reason": reason,
                "trace": _trace("persist_proposal", "failed", reason),
            }

    def _await_approval(self, state: MaintenanceGraphState) -> dict[str, object]:
        approval = interrupt(
            {
                "proposal_id": state["proposal_id"],
                "diff": (
                    state["evaluation_artifact"].evaluation.unified_diff
                    if state["evaluation_artifact"] is not None
                    else ""
                ),
                "status": "waiting_approval",
            }
        )
        approved = bool(
            approval.get("approved", False) if isinstance(approval, dict) else approval
        )
        return {
            "approval_status": "approved" if approved else "rejected",
            "status": "running" if approved else "failed",
            "stop_reason": "" if approved else "proposal rejected",
            "trace": _trace("await_approval", "approved" if approved else "rejected", str(state["proposal_id"])),
        }

    def _promote_patch(self, state: MaintenanceGraphState) -> dict[str, object]:
        self._emit("Maintenance promote_patch")
        proposal_id = state["proposal_id"]
        if proposal_id is None:
            return {"status": "failed", "stop_reason": "missing proposal_id"}
        try:
            result = self.promoter.promote(
                self.context,
                proposal_id,
                approved=state["approval_status"] == "approved",
            )
            return {
                "promotion_result": result,
                "trace": _trace("promote_patch", "completed", ",".join(result.changed_files)),
            }
        except Exception as exc:
            reason = f"promote_patch failed: {type(exc).__name__}: {exc}"
            return {
                "status": "failed",
                "stop_reason": reason,
                "trace": _trace("promote_patch", "failed", reason),
            }

    def _report_success(self, state: MaintenanceGraphState) -> dict[str, object]:
        proposal_id = state["proposal_id"] or ""
        report = "\n".join(
            [
                f"# RepoAgent maintenance result: {state['objective']}",
                "",
                f"- status: completed",
                f"- proposal_id: {proposal_id}",
                f"- attempts: {state['patch_attempt']}",
            ]
        )
        return {
            "status": "completed",
            "stop_reason": "approved patch promoted",
            "final_report": report,
            "trace": _trace("report_success", "completed", proposal_id),
        }

    def _report_failure(self, state: MaintenanceGraphState) -> dict[str, object]:
        reason = state["stop_reason"] or "patch attempts exhausted"
        return {
            "status": "failed",
            "stop_reason": reason,
            "final_report": f"# RepoAgent maintenance failed\n\n{reason}",
            "trace": _trace("report_failure", "failed", reason),
        }

    def _report_rejected(self, state: MaintenanceGraphState) -> dict[str, object]:
        proposal_id = state["proposal_id"] or ""
        return {
            "status": "failed",
            "stop_reason": "proposal rejected",
            "final_report": f"# RepoAgent maintenance rejected\n\nproposal_id: {proposal_id}",
            "trace": _trace("report_rejected", "rejected", proposal_id),
        }

    def _route_after_evaluate(
        self,
        state: MaintenanceGraphState,
    ) -> Literal["persist_proposal", "reflect_patch", "report_failure"]:
        if state["status"] == "failed":
            return "report_failure"
        artifact = state["evaluation_artifact"]
        if artifact is not None and artifact.evaluation.passed:
            return "persist_proposal"
        if state["patch_attempt"] >= self.config.max_patch_attempts:
            return "report_failure"
        return "reflect_patch"

    def _route_after_propose(
        self,
        state: MaintenanceGraphState,
    ) -> Literal["evaluate_patch", "report_failure"]:
        return "report_failure" if state["status"] == "failed" else "evaluate_patch"

    def _route_after_reflect(
        self,
        state: MaintenanceGraphState,
    ) -> Literal["propose_patch", "select_targets", "report_failure"]:
        if state["status"] == "failed":
            return "report_failure"
        reflection = state["reflection"]
        if reflection is None or reflection.next_action == "stop":
            return "report_failure"
        if reflection.next_action == "reselect":
            return "select_targets"
        return "propose_patch"

    def _route_after_approval(
        self,
        state: MaintenanceGraphState,
    ) -> Literal["promote_patch", "report_rejected"]:
        return (
            "promote_patch"
            if state["approval_status"] == "approved"
            else "report_rejected"
        )

    def run(
        self,
        objective: str,
        *,
        run_id: str | None = None,
        thread_id: str | None = None,
        checkpoint_thread_id: str | None = None,
    ) -> MaintenanceRunResult:
        resolved_run_id = run_id or str(uuid4())
        resolved_thread_id = thread_id or resolved_run_id
        initial_state: MaintenanceGraphState = {
            "run_id": resolved_run_id,
            "thread_id": resolved_thread_id,
            "project_id": self.context.project_id,
            "repo_root": str(self.context.repo_root),
            "repo_revision": self.context.revision,
            "objective": objective,
            "analysis": None,
            "selected_targets": None,
            "patch": None,
            "patch_history": [],
            "patch_fingerprints": [],
            "patch_attempt": 0,
            "evaluation_artifact": None,
            "evaluation_artifact_history": [],
            "reflection": None,
            "reflection_history": [],
            "proposal_id": None,
            "proposal_path": None,
            "approval_status": None,
            "promotion_result": None,
            "status": "running",
            "stop_reason": "",
            "final_report": "",
            "trace": [],
        }
        invoke_config: dict[str, Any] = {
            "recursion_limit": self.config.recursion_limit,
        }
        if checkpoint_thread_id is not None:
            invoke_config["configurable"] = {
                "thread_id": checkpoint_thread_id,
                "checkpoint_ns": "",
            }
        state = self.graph.invoke(initial_state, invoke_config)
        next_nodes: tuple[str, ...] = ()
        if checkpoint_thread_id is not None:
            next_nodes = self.graph.get_state(invoke_config).next
        return self.result_from_state(state, next_nodes=next_nodes)

    def resume(
        self,
        *,
        checkpoint_thread_id: str,
        approved: bool,
    ) -> MaintenanceRunResult:
        from langgraph.types import Command

        invoke_config: dict[str, Any] = {
            "recursion_limit": self.config.recursion_limit,
            "configurable": {
                "thread_id": checkpoint_thread_id,
                "checkpoint_ns": "",
            },
        }
        state = self.graph.invoke(Command(resume={"approved": approved}), invoke_config)
        return self.result_from_state(state)

    @staticmethod
    def result_from_state(
        state: dict[str, Any],
        *,
        next_nodes: tuple[str, ...] = (),
    ) -> MaintenanceRunResult:
        interrupted = bool(next_nodes) or "__interrupt__" in state
        status = "interrupted" if interrupted else state["status"]
        if interrupted and state.get("status") == "waiting_approval":
            status = "waiting_approval"
        artifact = state.get("evaluation_artifact")
        artifact_history = tuple(state.get("evaluation_artifact_history", ()))
        return MaintenanceRunResult(
            run_id=state["run_id"],
            thread_id=state["thread_id"],
            project_id=state["project_id"],
            repo_root=state["repo_root"],
            repo_revision=state["repo_revision"],
            objective=state["objective"],
            status=status,
            stop_reason=(
                "waiting for approval"
                if interrupted and status == "waiting_approval"
                else state.get("stop_reason", "")
            ),
            analysis=state.get("analysis"),
            selected_targets=state.get("selected_targets"),
            patch=state.get("patch"),
            patch_history=tuple(state.get("patch_history", ())),
            patch_attempt=state.get("patch_attempt", 0),
            evaluation=artifact.evaluation if artifact is not None else None,
            evaluation_history=tuple(item.evaluation for item in artifact_history),
            reflection=state.get("reflection"),
            reflection_history=tuple(state.get("reflection_history", ())),
            proposal_id=state.get("proposal_id"),
            approval_status=state.get("approval_status"),
            promotion_result=state.get("promotion_result"),
            final_report=state.get("final_report", ""),
            trace=tuple(state.get("trace", ())),
        )
