"""上下文压缩端口和不依赖外部模型的可审计压缩器。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .models import ContextPacket


class CompressionTokenCounter(Protocol):
    """压缩器所依赖的最小 Token 计数接口。"""

    def count(self, text: str) -> int:
        """返回文本的估算或精确 Token 数。"""


@dataclass(frozen=True, slots=True)
class CompressionRequest:
    """宿主交给压缩器的受限请求。"""

    packet: ContextPacket
    target_tokens: int
    attempt: int


@dataclass(frozen=True, slots=True)
class CompressedContent:
    """压缩器只能返回正文和策略，不能修改 Packet 安全元数据。"""

    content: str
    strategy: str


class ContextCompressor(Protocol):
    """高价值 Packet 超预算时使用的可替换压缩端口。"""

    def compress(
        self,
        request: CompressionRequest,
        token_counter: CompressionTokenCounter,
    ) -> CompressedContent | None:
        """把正文压到目标预算内；无法安全压缩时返回 None。"""


class ExtractiveContextCompressor:
    """保留证据首尾并显式标注省略范围的确定性压缩器。"""

    strategy = "extractive_head_tail_v1"

    @staticmethod
    def _render(content: str, retained_chars: int) -> str:
        """按七三比例保留首尾，避免把有损结果伪装成完整原文。"""

        if retained_chars >= len(content):
            return content
        head_size = max(1, round(retained_chars * 0.7))
        tail_size = max(0, retained_chars - head_size)
        omitted = len(content) - head_size - tail_size
        tail = content[-tail_size:] if tail_size else ""
        return (
            content[:head_size]
            + f"\n[...上下文压缩：省略 {omitted} 个字符...]\n"
            + tail
        )

    def compress(
        self,
        request: CompressionRequest,
        token_counter: CompressionTokenCounter,
    ) -> CompressedContent | None:
        """二分查找目标预算能容纳的最大首尾证据正文。"""

        packet = request.packet
        if packet.trust != "untrusted_evidence":
            return None
        original_tokens = token_counter.count(packet.content)
        if request.target_tokens < 8 or original_tokens <= request.target_tokens:
            return None

        low = 1
        high = len(packet.content) - 1
        best: str | None = None
        while low <= high:
            retained = (low + high) // 2
            candidate = self._render(packet.content, retained)
            if token_counter.count(candidate) <= request.target_tokens:
                best = candidate
                low = retained + 1
            else:
                high = retained - 1
        if best is None or not best.strip():
            return None
        if token_counter.count(best) >= original_tokens:
            return None
        return CompressedContent(content=best, strategy=self.strategy)
