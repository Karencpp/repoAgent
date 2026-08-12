"""根据显式配置创建结构化大模型供应商客户端。"""

from __future__ import annotations

import os
from typing import Literal, cast

from .contracts import LLMConfigurationError, StructuredJSONClient
from .deepseek import DeepSeekChatClient, DeepSeekConfig
from .glm import GLMChatClient, GLMConfig


LLMProviderName = Literal["glm", "deepseek"]


def resolve_llm_provider(value: str | None = None) -> LLMProviderName:
    """显式参数优先，其次读取环境变量，最后保持 GLM 兼容默认值。"""

    normalized = (value or os.environ.get("LLM_PROVIDER", "glm")).strip().casefold()
    if normalized not in {"glm", "deepseek"}:
        raise LLMConfigurationError(
            "LLM_PROVIDER 只能是 glm 或 deepseek"
        )
    return cast(LLMProviderName, normalized)


def structured_client_from_env(
    provider: str | None = None,
) -> StructuredJSONClient:
    """从选定供应商的环境配置创建客户端。"""

    resolved = resolve_llm_provider(provider)
    if resolved == "deepseek":
        return DeepSeekChatClient(DeepSeekConfig.from_env())
    return GLMChatClient(GLMConfig.from_env())
