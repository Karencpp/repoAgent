"""大模型供应商适配层使用的稳定协议。"""

from __future__ import annotations

from typing import Any, Literal, Mapping, Protocol

from pydantic import BaseModel, ConfigDict, Field


class ChatMessage(BaseModel):
    """一条与供应商无关的聊天消息。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    role: Literal["system", "user", "assistant"]
    content: str = Field(min_length=1, max_length=200_000)


class StructuredJSONRequest(BaseModel):
    """请求模型按照指定 JSON Schema 返回对象。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    messages: tuple[ChatMessage, ...] = Field(min_length=1, max_length=20)
    schema_name: str = Field(min_length=1, max_length=100)
    json_schema: dict[str, Any]


class StructuredJSONClient(Protocol):
    """Planner、ReAct 和 Reflector 共同依赖的结构化生成端口。"""

    def generate_json(self, request: StructuredJSONRequest) -> Mapping[str, Any]:
        """返回已经解析为对象、但尚未做领域 Schema 校验的数据。"""


class LLMProviderError(RuntimeError):
    """所有可预期供应商错误的基类。"""


class LLMConfigurationError(LLMProviderError):
    """本地配置缺失或不合法。"""


class LLMAuthenticationError(LLMProviderError):
    """API Key 无效或无权访问目标模型。"""


class LLMRateLimitError(LLMProviderError):
    """请求触发限流、配额或余额限制。"""


class LLMTimeoutError(LLMProviderError):
    """连接或读取供应商响应超时。"""


class LLMTransportError(LLMProviderError):
    """无法连接到供应商服务。"""


class LLMResponseError(LLMProviderError):
    """供应商返回了失败状态或不完整协议响应。"""


class LLMStructuredOutputError(LLMProviderError):
    """模型输出不是合法 JSON 对象或不满足领域 Schema。"""
