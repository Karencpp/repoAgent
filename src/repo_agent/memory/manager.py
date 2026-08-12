"""把工作流事实安全转换为长期记忆的生命周期服务。"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from typing import Literal, Protocol, Sequence

from repo_agent.llm.contracts import StructuredJSONClient
from repo_agent.projects import ProjectContext
from repo_agent.workflow.models import RepoAgentRunResult

from .curation import MemoryCurationError, MemoryCurator
from .formation import (
    MemoryCandidateExtractor,
    MemoryFormationError,
    PerceptualObservation,
    SemanticMemoryConsolidator,
    StructuredSemanticMemoryExtractor,
    WorkflowPerceptualMemoryExtractor,
    candidate_from_perceptual_observation,
)
from .models import (
    MemoryCandidate,
    MemoryCurationDecision,
    MemoryMaintenanceReport,
    MemoryRecord,
    MemoryWrite,
)
from .store import SQLiteMemoryStore


def _stable_identifier(prefix: str, *parts: str) -> str:
    """把可能含任意字符的业务标识转换成稳定且可校验的 ID。"""

    payload = "\0".join(parts).encode("utf-8")
    return f"{prefix}:{sha256(payload).hexdigest()}"


class WorkflowRunner(Protocol):
    """Memory 装饰器所依赖的最小工作流接口。"""

    def run(
        self,
        context: ProjectContext,
        user_goal: str,
        *,
        mode: Literal["diagnose", "fix"] = "diagnose",
        run_id: str | None = None,
        thread_id: str | None = None,
        checkpoint_thread_id: str | None = None,
    ) -> RepoAgentRunResult: ...


@dataclass(frozen=True, slots=True)
class MemoryAwareRunResult:
    """一次工作流运行及三类记忆形成结果。"""

    workflow_result: RepoAgentRunResult
    memory_decision: MemoryCurationDecision
    formation_decisions: tuple[MemoryCurationDecision, ...] = ()
    formation_errors: tuple[str, ...] = ()


class MemoryManager:
    """集中管理记忆候选、审核、维护和显式遗忘。"""

    def __init__(
        self,
        store: SQLiteMemoryStore,
        *,
        curator: MemoryCurator | None = None,
    ) -> None:
        self.store = store
        self.curator = curator or MemoryCurator(store)

    @staticmethod
    def _record_from_decision(
        store: SQLiteMemoryStore,
        context: ProjectContext,
        decision: MemoryCurationDecision,
    ) -> MemoryRecord:
        """兼容旧接口：把成功治理决策还原成最终活动记忆。"""

        if decision.result_memory_id is None:
            raise MemoryCurationError(
                f"候选没有形成可用记忆：{decision.action}，{decision.reason}"
            )
        return store.get(context, decision.result_memory_id)

    def curate_run(
        self,
        context: ProjectContext,
        result: RepoAgentRunResult,
        *,
        importance: float = 0.7,
    ) -> MemoryCurationDecision:
        """在工作流产生客观结果后，幂等提交一条情景记忆候选。"""

        if result.project_id != context.project_id:
            raise ValueError("工作流结果与 Memory ProjectContext 不属于同一项目")
        if result.repo_revision != context.revision:
            raise ValueError("工作流结果与 Memory ProjectContext 版本不一致")
        evaluation_summary = (
            result.evaluation.summary if result.evaluation is not None else "无评估结果"
        )
        content = (
            f"任务：{result.user_goal}\n"
            f"运行状态：{result.status}\n"
            f"停止原因：{result.stop_reason}\n"
            f"评估记录：{evaluation_summary}"
        )
        evidence = [
            f"run:{result.run_id}",
            f"thread:{result.thread_id}",
            f"revision:{result.repo_revision}",
        ]
        if result.evaluation is not None:
            evidence.extend(result.evaluation.evidence)
        run_identity = _stable_identifier("run", result.run_id)
        return self.curator.submit(
            context,
            MemoryCandidate(
                candidate_id=run_identity,
                memory_key=run_identity,
                proposed_by="workflow",
                rationale="工作流完成后保留可追溯的任务结果和客观评估",
                memory=MemoryWrite(
                    memory_type="episodic",
                    content=content,
                    claim_status="verified",
                    importance=importance,
                    scope="revision",
                    repo_revision=context.revision,
                    source="workflow",
                    source_id=result.run_id,
                    evidence=tuple(evidence),
                    tags=("workflow-run", result.mode, result.status),
                ),
            ),
        )

    def record_run(
        self,
        context: ProjectContext,
        result: RepoAgentRunResult,
        *,
        importance: float = 0.7,
    ) -> MemoryRecord:
        """记录工作流结果；同一 run_id 重放不会重复写入。"""

        decision = self.curate_run(context, result, importance=importance)
        return self._record_from_decision(self.store, context, decision)

    def submit_candidate(
        self,
        context: ProjectContext,
        candidate: MemoryCandidate,
    ) -> MemoryCurationDecision:
        """提交由用户、系统、模型或工作流提出的通用记忆候选。"""

        return self.curator.submit(context, candidate)

    def review_candidate(
        self,
        context: ProjectContext,
        candidate_id: str,
        *,
        approve: bool,
        reviewer: str,
        reason: str,
    ) -> MemoryCurationDecision:
        """批准或拒绝等待审核的高风险候选。"""

        return self.curator.review(
            context,
            candidate_id,
            approve=approve,
            reviewer=reviewer,
            reason=reason,
        )

    def pending_reviews(
        self,
        context: ProjectContext,
        *,
        limit: int = 100,
    ) -> tuple[MemoryCurationDecision, ...]:
        """列出当前项目等待人工处理的记忆候选。"""

        return self.curator.pending_reviews(context, limit=limit)

    def remember_verified_fact(
        self,
        context: ProjectContext,
        content: str,
        *,
        evidence: tuple[str, ...],
        source_id: str,
        memory_key: str | None = None,
        scope: Literal["project", "revision"] = "revision",
        importance: float = 0.8,
        tags: tuple[str, ...] = (),
    ) -> MemoryRecord:
        """显式提交有来源的语义事实，并经过冲突与重复治理。"""

        resolved_key = memory_key or _stable_identifier("fact", source_id)
        candidate_id = _stable_identifier("candidate", source_id, content)
        decision = self.curator.submit(
            context,
            MemoryCandidate(
                candidate_id=candidate_id,
                memory_key=resolved_key,
                proposed_by="manual",
                rationale="调用方显式提交了有证据的已验证事实",
                memory=MemoryWrite(
                    memory_type="semantic",
                    content=content,
                    claim_status="verified",
                    importance=importance,
                    scope=scope,
                    repo_revision=(context.revision if scope == "revision" else None),
                    source="manual",
                    source_id=source_id,
                    evidence=evidence,
                    tags=tags,
                ),
            ),
        )
        return self._record_from_decision(self.store, context, decision)

    def remember_hypothesis(
        self,
        context: ProjectContext,
        content: str,
        *,
        source_id: str,
        memory_key: str | None = None,
        evidence: tuple[str, ...] = (),
        importance: float = 0.5,
        tags: tuple[str, ...] = (),
    ) -> MemoryRecord:
        """提交尚未验证的结论；策略会为它设置较短 TTL。"""

        resolved_key = memory_key or _stable_identifier("hypothesis", source_id)
        candidate_id = _stable_identifier("candidate", source_id, content)
        decision = self.curator.submit(
            context,
            MemoryCandidate(
                candidate_id=candidate_id,
                memory_key=resolved_key,
                proposed_by="manual",
                rationale="调用方显式记录了需要后续验证的推理假设",
                memory=MemoryWrite(
                    memory_type="semantic",
                    content=content,
                    claim_status="hypothesis",
                    importance=importance,
                    scope="revision",
                    repo_revision=context.revision,
                    source="manual",
                    source_id=source_id,
                    evidence=evidence,
                    tags=tags,
                ),
            ),
        )
        return self._record_from_decision(self.store, context, decision)

    def remember_perceptual_observation(
        self,
        context: ProjectContext,
        observation: PerceptualObservation,
    ) -> MemoryCurationDecision:
        """把工具、模型、用户或系统产生的制品观察交给 Curator。"""

        return self.curator.submit(
            context,
            candidate_from_perceptual_observation(context, observation),
        )

    def consolidate_semantic_memories(
        self,
        context: ProjectContext,
        topic: str,
        *,
        client: StructuredJSONClient,
        top_k: int = 10,
    ) -> tuple[MemoryCurationDecision, ...]:
        """从多次已验证情景中归纳语义候选，并继续走审核策略。"""

        return SemanticMemoryConsolidator(
            self.store,
            self.curator,
            client,
        ).consolidate(context, topic, top_k=top_k)

    def forget(
        self,
        context: ProjectContext,
        memory_id: str,
        *,
        requested_by: str,
        reason: str,
    ) -> None:
        """按显式主体和原因遗忘记忆，只保留不可召回的审计墓碑。"""

        if not requested_by.strip() or not reason.strip():
            raise ValueError("遗忘请求人和原因不能为空")
        self.store.forget(
            context,
            memory_id,
            actor=requested_by,
            reason=reason,
        )

    def run_maintenance(self, context: ProjectContext) -> MemoryMaintenanceReport:
        """执行 TTL 清理；即使未清理，过期项也不会参与检索。"""

        return self.store.expire(context)


class MemoryAwareWorkflowRunner:
    """在工作流完成后自动形成情景、语义和感知记忆候选。"""

    def __init__(
        self,
        workflow: WorkflowRunner,
        memory: MemoryManager,
        *,
        semantic_client: StructuredJSONClient | None = None,
        candidate_extractors: Sequence[MemoryCandidateExtractor] = (),
        trusted_perception_tools: Sequence[str] = (),
    ) -> None:
        self.workflow = workflow
        self.memory = memory
        extractors: list[MemoryCandidateExtractor] = []
        if semantic_client is not None:
            extractors.append(StructuredSemanticMemoryExtractor(semantic_client))
        extractors.append(
            WorkflowPerceptualMemoryExtractor(
                trusted_verified_tools=tuple(trusted_perception_tools)
            )
        )
        extractors.extend(candidate_extractors)
        self.candidate_extractors = tuple(extractors)

    def run(
        self,
        context: ProjectContext,
        user_goal: str,
        *,
        mode: Literal["diagnose", "fix"] = "diagnose",
        run_id: str | None = None,
        thread_id: str | None = None,
        checkpoint_thread_id: str | None = None,
        memory_importance: float = 0.7,
    ) -> MemoryAwareRunResult:
        """运行主图并自动保存经过治理的任务记忆。"""

        self.memory.run_maintenance(context)
        workflow_result = self.workflow.run(
            context,
            user_goal,
            mode=mode,
            run_id=run_id,
            thread_id=thread_id,
            checkpoint_thread_id=checkpoint_thread_id,
        )
        episodic_decision = self.memory.curate_run(
            context,
            workflow_result,
            importance=memory_importance,
        )
        formation_decisions: list[MemoryCurationDecision] = []
        formation_errors: list[str] = []
        for extractor in self.candidate_extractors:
            try:
                candidates = extractor.extract(context, workflow_result)
            except MemoryFormationError as exc:
                formation_errors.append(f"{extractor.name}: {exc}")
                continue
            for candidate in candidates:
                formation_decisions.append(
                    self.memory.submit_candidate(context, candidate)
                )
        return MemoryAwareRunResult(
            workflow_result=workflow_result,
            memory_decision=episodic_decision,
            formation_decisions=tuple(formation_decisions),
            formation_errors=tuple(formation_errors),
        )
