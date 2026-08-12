"""上下文工程使用的来源包、信任级别和选择结果。"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


ContextSource = Literal[
    "system",
    "task",
    "working_state",
    "episodic_memory",
    "semantic_memory",
    "perceptual_memory",
    "rag",
    "tool_observation",
    "skill",
]
ContextTrust = Literal[
    "trusted_instruction",
    "user_request",
    "trusted_state",
    "untrusted_evidence",
]


class ContextModel(BaseModel):
    """上下文模型的严格公共配置。"""

    model_config = ConfigDict(extra="forbid", frozen=True)


class ContextPacket(ContextModel):
    """带来源、优先级和信任边界的最小上下文单元。"""

    packet_id: str = Field(min_length=1, max_length=200)
    source: ContextSource
    trust: ContextTrust
    content: str = Field(min_length=1, max_length=100_000)
    priority: int = Field(default=50, ge=0, le=100)
    mandatory: bool = False
    citations: tuple[str, ...] = Field(default=(), max_length=100)
    dedupe_key: str | None = Field(default=None, max_length=500)
    created_at: datetime | None = None

    @model_validator(mode="after")
    def validate_source_trust(self) -> "ContextPacket":
        """防止外部资料伪装成系统指令或内部状态。"""

        allowed = {
            "system": {"trusted_instruction"},
            "skill": {"trusted_instruction"},
            "task": {"user_request"},
            "working_state": {"trusted_state"},
            "episodic_memory": {"untrusted_evidence"},
            "semantic_memory": {"untrusted_evidence"},
            "perceptual_memory": {"untrusted_evidence"},
            "rag": {"untrusted_evidence"},
            "tool_observation": {"untrusted_evidence"},
        }
        if self.trust not in allowed[self.source]:
            raise ValueError(
                f"上下文来源 {self.source} 不能使用信任级别 {self.trust}"
            )
        return self


class ContextSelection(ContextModel):
    """单个 Packet 是否进入模型上下文的审计结果。"""

    packet_id: str
    included: bool
    estimated_tokens: int = Field(ge=0)
    reason: Literal[
        "included",
        "duplicate",
        "budget_exceeded",
        "compressed",
    ]
    replacement_packet_id: str | None = Field(default=None, max_length=200)

    @model_validator(mode="after")
    def validate_replacement(self) -> "ContextSelection":
        """只有压缩决策可以指向替代 Packet。"""

        if self.reason == "compressed" and not self.replacement_packet_id:
            raise ValueError("compressed 选择必须提供 replacement_packet_id")
        if self.reason != "compressed" and self.replacement_packet_id is not None:
            raise ValueError("非压缩选择不能提供 replacement_packet_id")
        return self


class ContextCompression(ContextModel):
    """一次可追溯的 Packet 压缩及重新预算结果。"""

    source_packet_id: str = Field(min_length=1, max_length=200)
    compressed_packet_id: str = Field(min_length=1, max_length=200)
    source: ContextSource
    trust: ContextTrust
    strategy: str = Field(min_length=1, max_length=100)
    original_tokens: int = Field(ge=1)
    target_tokens: int = Field(ge=1)
    compressed_tokens: int = Field(ge=1)
    attempts: int = Field(ge=1, le=10)
    citations: tuple[str, ...] = Field(default=(), max_length=100)

    @model_validator(mode="after")
    def validate_token_reduction(self) -> "ContextCompression":
        """压缩审计必须证明正文 Token 确实减少。"""

        if self.compressed_tokens >= self.original_tokens:
            raise ValueError("压缩后的 Token 数必须小于原始 Token 数")
        return self


class BuiltContext(ContextModel):
    """一次模型调用可直接使用的已选择上下文。"""

    content: str
    model_context_window: int = Field(ge=1)
    reserved_output_tokens: int = Field(ge=0)
    input_budget_tokens: int = Field(ge=1)
    estimated_input_tokens: int = Field(ge=0)
    selections: tuple[ContextSelection, ...]
    compressions: tuple[ContextCompression, ...] = ()

    @property
    def included_packet_ids(self) -> tuple[str, ...]:
        """返回进入上下文的 Packet 标识。"""

        return tuple(
            item.packet_id for item in self.selections if item.included
        )

    @property
    def excluded_packet_ids(self) -> tuple[str, ...]:
        """返回因去重或预算未进入上下文的 Packet 标识。"""

        return tuple(
            item.packet_id for item in self.selections if not item.included
        )

    @property
    def compressed_packet_ids(self) -> tuple[str, ...]:
        """返回发生过有损压缩的原始 Packet 标识。"""

        return tuple(item.source_packet_id for item in self.compressions)
