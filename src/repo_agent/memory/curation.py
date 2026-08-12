"""长期记忆候选的确定性策略、人工审核与幂等落库。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import re
import unicodedata
from typing import Callable

from repo_agent.projects import ProjectContext

from .models import (
    MemoryCandidate,
    MemoryCurationDecision,
    MemoryRecord,
    MemoryWrite,
)
from .store import SQLiteMemoryStore


class MemoryCurationError(RuntimeError):
    """长期记忆候选决策失败。"""


class MemoryCurationConflictError(MemoryCurationError):
    """等待审核期间活动事实已经变化。"""


@dataclass(frozen=True, slots=True)
class MemoryCurationPolicy:
    """控制自动接纳、默认保留期和人工审核边界。"""

    min_importance: float = 0.2
    episodic_ttl_days: int = 180
    hypothesis_ttl_days: int = 14
    perceptual_ttl_days: int = 90
    require_review_for_verified_change: bool = True

    def __post_init__(self) -> None:
        if not 0 <= self.min_importance <= 1:
            raise ValueError("min_importance 必须在 0 到 1 之间")
        if min(
            self.episodic_ttl_days,
            self.hypothesis_ttl_days,
            self.perceptual_ttl_days,
        ) < 1:
            raise ValueError("默认记忆保留天数必须大于 0")


_WHITESPACE_PATTERN = re.compile(r"\s+")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _normalized_content(content: str) -> str:
    """规范化事实正文，用于确定性去重而不是语义判等。"""

    normalized = unicodedata.normalize("NFKC", content).casefold().strip()
    return _WHITESPACE_PATTERN.sub(" ", normalized)


def _equivalent(record: MemoryRecord, memory: MemoryWrite) -> bool:
    """判断候选是否与活动记录在事实和审计字段上完全等价。"""

    return (
        _normalized_content(record.content) == _normalized_content(memory.content)
        and record.memory_type == memory.memory_type
        and record.claim_status == memory.claim_status
        and record.scope == memory.scope
        and record.repo_revision == memory.repo_revision
        and set(record.evidence) == set(memory.evidence)
        and set(record.tags) == set(memory.tags)
        and record.importance == memory.importance
    )


class MemoryCurator:
    """把候选转换为创建、忽略、替代、审核或拒绝决策。"""

    def __init__(
        self,
        store: SQLiteMemoryStore,
        *,
        policy: MemoryCurationPolicy | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.store = store
        self.policy = policy or MemoryCurationPolicy()
        self.clock = clock or _utc_now

    def _apply_default_ttl(self, candidate: MemoryCandidate) -> MemoryCandidate:
        """只在候选没有显式 TTL 时应用类型策略。"""

        memory = candidate.memory
        if memory.expires_at is not None:
            return candidate
        days: int | None = None
        if memory.claim_status == "hypothesis":
            days = self.policy.hypothesis_ttl_days
        elif memory.memory_type == "episodic":
            days = self.policy.episodic_ttl_days
        elif memory.memory_type == "perceptual":
            days = self.policy.perceptual_ttl_days
        if days is None:
            return candidate
        expires_at = self.clock() + timedelta(days=days)
        return candidate.model_copy(
            update={"memory": memory.model_copy(update={"expires_at": expires_at})}
        )

    def _decision(
        self,
        context: ProjectContext,
        candidate: MemoryCandidate,
        action: str,
        reason: str,
        *,
        matched: MemoryRecord | None = None,
        result: MemoryRecord | None = None,
        decided_by: str | None = "memory-policy",
        created_at: datetime | None = None,
    ) -> MemoryCurationDecision:
        """构造并持久化一条可审计决策。"""

        now = self.clock()
        decision = MemoryCurationDecision(
            project_id=context.project_id,
            candidate=candidate,
            action=action,
            reason=reason,
            matched_memory_id=matched.memory_id if matched else None,
            result_memory_id=result.memory_id if result else None,
            decided_by=decided_by,
            created_at=created_at or now,
            updated_at=now,
        )
        return self.store.save_curation_decision(
            decision,
            replace=created_at is not None,
        )

    @staticmethod
    def _validate_context(
        context: ProjectContext,
        candidate: MemoryCandidate,
    ) -> str | None:
        """返回候选与当前项目不一致的原因。"""

        memory = candidate.memory
        if memory.scope == "revision" and memory.repo_revision != context.revision:
            return "候选 revision 与当前项目版本不一致"
        return None

    def submit(
        self,
        context: ProjectContext,
        candidate: MemoryCandidate,
    ) -> MemoryCurationDecision:
        """幂等提交候选，并按策略决定自动动作或进入审核。"""

        existing_decision = self.store.get_curation_decision(
            context,
            candidate.candidate_id,
        )
        if existing_decision is not None:
            return existing_decision

        candidate = self._apply_default_ttl(candidate)
        invalid_reason = self._validate_context(context, candidate)
        if invalid_reason is not None:
            return self._decision(
                context,
                candidate,
                "rejected",
                invalid_reason,
            )
        if candidate.memory.importance < self.policy.min_importance:
            return self._decision(
                context,
                candidate,
                "rejected",
                "候选重要性低于持久化阈值",
            )

        self.store.expire(context, now=self.clock())
        matched = self.store.find_active_by_key(context, candidate.memory_key)
        if matched is not None and _equivalent(matched, candidate.memory):
            return self._decision(
                context,
                candidate,
                "ignored_duplicate",
                "相同事实键已经存在完全等价的活动记忆",
                matched=matched,
                result=matched,
            )

        if candidate.proposed_by == "model" and candidate.memory.claim_status == "verified":
            return self._decision(
                context,
                candidate,
                "pending_review",
                "模型提出的 verified 事实必须由宿主或用户审核",
                matched=matched,
                decided_by=None,
            )

        if matched is None:
            result = self.store.put_with_key(
                context,
                candidate.memory,
                memory_key=candidate.memory_key,
            )
            return self._decision(
                context,
                candidate,
                "created",
                "候选通过硬约束且事实键尚无活动记忆",
                result=result,
            )

        can_auto_promote = (
            matched.claim_status in {"hypothesis", "refuted"}
            and candidate.memory.claim_status == "verified"
            and candidate.proposed_by in {"workflow", "system", "user", "manual"}
        )
        can_auto_refresh_hypothesis = (
            matched.claim_status == "hypothesis"
            and candidate.memory.claim_status == "hypothesis"
        )
        if can_auto_promote or can_auto_refresh_hypothesis:
            result = self.store.supersede(
                context,
                matched.memory_id,
                candidate.memory,
            )
            return self._decision(
                context,
                candidate,
                "superseded",
                (
                    "带证据事实提升了旧假设或被否定记录"
                    if can_auto_promote
                    else "同一事实键的新假设替代旧假设"
                ),
                matched=matched,
                result=result,
            )

        if self.policy.require_review_for_verified_change:
            return self._decision(
                context,
                candidate,
                "pending_review",
                "同一事实键的活动记忆内容发生变化，需要人工审核",
                matched=matched,
                decided_by=None,
            )

        result = self.store.supersede(
            context,
            matched.memory_id,
            candidate.memory,
        )
        return self._decision(
            context,
            candidate,
            "superseded",
            "策略允许自动替代同一事实键的活动记忆",
            matched=matched,
            result=result,
        )

    def review(
        self,
        context: ProjectContext,
        candidate_id: str,
        *,
        approve: bool,
        reviewer: str,
        reason: str,
    ) -> MemoryCurationDecision:
        """审核等待中的候选，并防止审核期间事实版本静默变化。"""

        current = self.store.get_curation_decision(context, candidate_id)
        if current is None:
            raise MemoryCurationError(f"找不到候选：{candidate_id}")
        if current.action != "pending_review":
            return current
        if not reviewer.strip() or not reason.strip():
            raise ValueError("审核人和审核原因不能为空")
        if not approve:
            return self._decision(
                context,
                current.candidate,
                "rejected",
                reason,
                matched=(
                    self.store.get(context, current.matched_memory_id)
                    if current.matched_memory_id
                    else None
                ),
                decided_by=reviewer,
                created_at=current.created_at,
            )

        matched = self.store.find_active_by_key(
            context,
            current.candidate.memory_key,
        )
        if current.matched_memory_id is None:
            if matched is not None:
                raise MemoryCurationConflictError(
                    "候选等待审核期间出现了新的活动记忆，请重新提交候选"
                )
            result = self.store.put_with_key(
                context,
                current.candidate.memory,
                memory_key=current.candidate.memory_key,
            )
            action = "created"
        else:
            if matched is None or matched.memory_id != current.matched_memory_id:
                raise MemoryCurationConflictError(
                    "候选等待审核期间活动记忆已经变化，请重新提交候选"
                )
            if _equivalent(matched, current.candidate.memory):
                result = matched
                action = "ignored_duplicate"
            else:
                result = self.store.supersede(
                    context,
                    matched.memory_id,
                    current.candidate.memory,
                )
                action = "superseded"
        return self._decision(
            context,
            current.candidate,
            action,
            reason,
            matched=matched,
            result=result,
            decided_by=reviewer,
            created_at=current.created_at,
        )

    def pending_reviews(
        self,
        context: ProjectContext,
        *,
        limit: int = 100,
    ) -> tuple[MemoryCurationDecision, ...]:
        """返回等待宿主或用户处理的候选。"""

        return self.store.list_pending_reviews(context, limit=limit)
