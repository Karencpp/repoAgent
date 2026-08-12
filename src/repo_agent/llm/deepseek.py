"""DeepSeek 对话补全接口的结构化 JSON HTTP 适配器。"""

from __future__ import annotations

import json
import os
from typing import Any, Mapping
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError

from .contracts import (
    LLMAuthenticationError,
    LLMConfigurationError,
    LLMRateLimitError,
    LLMResponseError,
    LLMStructuredOutputError,
    LLMTimeoutError,
    LLMTransportError,
    StructuredJSONRequest,
)


DEFAULT_DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEFAULT_DEEPSEEK_MODEL = "deepseek-chat"


class DeepSeekConfig(BaseModel):
    """保存 DeepSeek 调用配置，并在对象表示中隐藏密钥。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    api_key: SecretStr
    model: str = Field(default=DEFAULT_DEEPSEEK_MODEL, min_length=1, max_length=100)
    base_url: str = DEFAULT_DEEPSEEK_BASE_URL
    timeout_seconds: float = Field(default=60.0, gt=0, le=180)
    max_tokens: int = Field(default=4_000, ge=128, le=32_768)
    temperature: float = Field(default=0.1, ge=0, le=2)

    def model_post_init(self, __context: Any) -> None:
        """尽早拒绝空密钥和非 HTTPS 远程地址。"""

        if not self.api_key.get_secret_value().strip():
            raise ValueError("api_key 不能为空")
        parsed = urlparse(self.base_url)
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValueError("base_url 必须是有效的 HTTPS 地址")

    @property
    def endpoint(self) -> str:
        """返回兼容带或不带版本前缀的对话补全地址。"""

        base = self.base_url.rstrip("/")
        if base.endswith("/chat/completions"):
            return base
        return f"{base}/chat/completions"

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "DeepSeekConfig":
        """只从环境变量读取 DeepSeek 密钥和可选模型配置。"""

        source = os.environ if environ is None else environ
        api_key = source.get("DEEPSEEK_API_KEY", "").strip()
        if not api_key:
            raise LLMConfigurationError("缺少环境变量 DEEPSEEK_API_KEY")
        try:
            return cls(
                api_key=SecretStr(api_key),
                model=source.get("DEEPSEEK_MODEL", DEFAULT_DEEPSEEK_MODEL),
                base_url=source.get("DEEPSEEK_BASE_URL", DEFAULT_DEEPSEEK_BASE_URL),
            )
        except ValidationError as exc:
            raise LLMConfigurationError(
                f"DeepSeek 环境配置不合法：{exc.error_count()} 个错误"
            ) from exc


class _DeepSeekMessage(BaseModel):
    """DeepSeek 响应中的消息片段。"""

    content: str | None = None


class _DeepSeekChoice(BaseModel):
    """DeepSeek 响应中的候选结果。"""

    message: _DeepSeekMessage
    finish_reason: str | None = None


class _DeepSeekResponse(BaseModel):
    """适配器真正依赖的最小响应结构。"""

    id: str | None = None
    model: str | None = None
    choices: tuple[_DeepSeekChoice, ...] = Field(min_length=1)


def _safe_error_message(response: httpx.Response, api_key: str) -> str:
    """提取有限错误信息，并移除可能被服务端回显的密钥。"""

    message = "供应商未返回错误说明"
    try:
        payload = response.json()
        if isinstance(payload, Mapping):
            error = payload.get("error")
            if isinstance(error, Mapping):
                message = str(error.get("message") or error.get("msg") or message)
            else:
                message = str(payload.get("message") or payload.get("msg") or message)
    except (ValueError, TypeError):
        if response.text.strip():
            message = response.text.strip()
    return message.replace(api_key, "***")[:500]


class DeepSeekChatClient:
    """调用 DeepSeek JSON Output，并统一翻译供应商错误。"""

    def __init__(
        self,
        config: DeepSeekConfig,
        *,
        http_client: httpx.Client | None = None,
    ) -> None:
        self.config = config
        self._owns_client = http_client is None
        self._client = http_client or httpx.Client(timeout=config.timeout_seconds)

    def close(self) -> None:
        """只关闭由适配器自行创建的连接池。"""

        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "DeepSeekChatClient":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def generate_json(self, request: StructuredJSONRequest) -> Mapping[str, Any]:
        """请求非流式 JSON Output，并把内容解析成普通映射。"""

        api_key = self.config.api_key.get_secret_value()
        body = {
            "model": self.config.model,
            "messages": [message.model_dump() for message in request.messages],
            "response_format": {"type": "json_object"},
            "stream": False,
            "temperature": self.config.temperature,
            "max_tokens": self.config.max_tokens,
        }
        try:
            response = self._client.post(
                self.config.endpoint,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json=body,
                timeout=self.config.timeout_seconds,
            )
        except httpx.TimeoutException as exc:
            raise LLMTimeoutError("DeepSeek 请求超时") from exc
        except httpx.RequestError as exc:
            raise LLMTransportError(
                f"DeepSeek 网络请求失败：{type(exc).__name__}"
            ) from exc

        if response.status_code in {401, 403}:
            raise LLMAuthenticationError(
                f"DeepSeek 鉴权失败（HTTP {response.status_code}）："
                f"{_safe_error_message(response, api_key)}"
            )
        if response.status_code == 429:
            raise LLMRateLimitError(
                "DeepSeek 请求受到限流、余额或配额限制："
                f"{_safe_error_message(response, api_key)}"
            )
        if response.is_error:
            raise LLMResponseError(
                f"DeepSeek 返回 HTTP {response.status_code}："
                f"{_safe_error_message(response, api_key)}"
            )

        try:
            envelope = _DeepSeekResponse.model_validate(response.json())
        except (ValueError, ValidationError) as exc:
            raise LLMResponseError("DeepSeek 返回的协议结构不完整") from exc
        choice = envelope.choices[0]
        if choice.finish_reason == "length":
            raise LLMStructuredOutputError("DeepSeek JSON 因输出长度限制被截断")
        content = choice.message.content
        if content is None or not content.strip():
            raise LLMStructuredOutputError("DeepSeek 返回了空 JSON 内容")
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError as exc:
            raise LLMStructuredOutputError("DeepSeek 未返回合法 JSON") from exc
        if not isinstance(parsed, dict):
            raise LLMStructuredOutputError("DeepSeek JSON 顶层必须是对象")
        return parsed
