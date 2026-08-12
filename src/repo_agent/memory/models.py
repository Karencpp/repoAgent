"""长期记忆的领域模型与生命周期约束。"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


PersistedMemoryType = Literal["episodic", "semantic", "perceptual"]
MemoryClaimStatus = Literal["hypothesis", "verified", "refuted"]
MemoryScope = Literal["project", "revision"]
MemoryStatus = Literal["active", "superseded", "forgotten", "expired"]
MemorySource = Literal[
    "user",
    "workflow",
    "evaluator",
    "tool",
    "model",
    "system",
    "manual",
]
MemoryProposer = Literal["workflow", "tool", "user", "system", "model", "manual"]
MemoryCurationAction = Literal[
    "created",
    "ignored_duplicate",
    "superseded",
    "pending_review",
    "rejected",
]
MemoryLifecycleEventType = Literal["forgotten", "expired"]


class MemoryModel(BaseModel):
    """记忆模型的公共严格配置。"""

    model_config = ConfigDict(extra="forbid", frozen=True)


class MemoryWrite(MemoryModel):
    """写入长期记忆所需的显式事实、来源和有效范围。"""

    memory_type: PersistedMemoryType
    content: str = Field(min_length=1, max_length=20_000)
    claim_status: MemoryClaimStatus
    importance: float = Field(default=0.5, ge=0, le=1)
    scope: MemoryScope = "project"
    repo_revision: str | None = Field(default=None, max_length=500)
    source: MemorySource
    source_id: str = Field(min_length=1, max_length=500)
    evidence: tuple[str, ...] = Field(default=(), max_length=50)
    tags: tuple[str, ...] = Field(default=(), max_length=30)
    expires_at: datetime | None = None

    @model_validator(mode="after")
    def validate_memory_semantics(self) -> "MemoryWrite":
        """防止把无证据结论和无版本事实写成可信长期记忆。"""

        if self.scope == "revision" and not self.repo_revision:
            raise ValueError("revision 范围的记忆必须提供 repo_revision")
        if self.claim_status == "verified" and not self.evidence:
            raise ValueError("verified 记忆必须提供 evidence")
        if self.memory_type == "perceptual" and not self.evidence:
            raise ValueError("perceptual 记忆必须引用原始感知产物")
        normalized_tags = [tag.strip() for tag in self.tags]
        if any(not tag or len(tag) > 100 for tag in normalized_tags):
            raise ValueError("tag 长度必须为 1 到 100")
        if len(set(normalized_tags)) != len(normalized_tags):
            raise ValueError("tags 不能重复")
        return self


class MemoryRecord(MemoryModel):
    """已经持久化并可审计的长期记忆。"""

    memory_id: str
    project_id: str
    memory_key: str | None = None
    memory_type: PersistedMemoryType
    content: str
    claim_status: MemoryClaimStatus
    importance: float
    scope: MemoryScope
    repo_revision: str | None
    source: MemorySource
    source_id: str
    evidence: tuple[str, ...]
    tags: tuple[str, ...]
    status: MemoryStatus
    supersedes_memory_id: str | None = None
    created_at: datetime
    updated_at: datetime
    expires_at: datetime | None = None
    embedding_model: str
    embedding_dimensions: int = Field(ge=1)


class MemoryCandidate(MemoryModel):
    """进入 Curator 的候选记忆，而不是已经获准写入的事实。"""

    candidate_id: str = Field(
        min_length=1,
        max_length=300,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9:._/-]*$",
    )
    memory_key: str = Field(
        min_length=1,
        max_length=300,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9:._/-]*$",
    )
    proposed_by: MemoryProposer
    rationale: str = Field(min_length=1, max_length=2_000)
    memory: MemoryWrite


class MemoryCurationDecision(MemoryModel):
    """Curator 对候选记忆的持久化决策与审计结果。"""

    project_id: str
    candidate: MemoryCandidate
    action: MemoryCurationAction
    reason: str = Field(min_length=1, max_length=2_000)
    matched_memory_id: str | None = None
    result_memory_id: str | None = None
    decided_by: str | None = Field(default=None, max_length=300)
    created_at: datetime
    updated_at: datetime


class MemoryLifecycleEvent(MemoryModel):
    """遗忘与过期操作留下的最小审计事件。"""

    event_id: str
    project_id: str
    memory_id: str
    event_type: MemoryLifecycleEventType
    actor: str = Field(min_length=1, max_length=300)
    reason: str = Field(min_length=1, max_length=2_000)
    created_at: datetime


class MemorySearchRequest(MemoryModel):
    """带类型、可信度、重要性和版本过滤的记忆查询。"""

    query: str = Field(min_length=1, max_length=2_000)
    memory_types: tuple[PersistedMemoryType, ...] = Field(
        default=("episodic", "semantic", "perceptual"),
        min_length=1,
    )
    claim_statuses: tuple[MemoryClaimStatus, ...] = Field(
        default=("verified",),
        min_length=1,
    )
    min_importance: float = Field(default=0.0, ge=0, le=1)
    top_k: int = Field(default=5, ge=1, le=20)
    include_stale_revisions: bool = False


class MemoryHit(MemoryModel):
    """带融合排名和版本新鲜度的长期记忆命中。"""

    record: MemoryRecord
    score: float = Field(ge=0)
    lexical_rank: int | None = Field(default=None, ge=1)
    dense_rank: int | None = Field(default=None, ge=1)
    stale_revision: bool


class MemorySearchResult(MemoryModel):
    """一次按项目隔离的记忆检索结果。"""

    project_id: str
    repo_revision: str
    query: str
    hits: tuple[MemoryHit, ...]
    embedding_model: str


class MemoryMaintenanceReport(MemoryModel):
    """过期、遗忘或重建操作的统计。"""

    project_id: str
    expired_count: int = Field(ge=0)
    forgotten_count: int = Field(ge=0)
    reembedded_count: int = Field(ge=0)
