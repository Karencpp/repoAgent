"""智谱 GLM 对话补全接口的最小 HTTP 适配器。"""

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


DEFAULT_GLM_BASE_URL = "https://open.bigmodel.cn/api/paas/v4"
DEFAULT_GLM_MODEL = "glm-4.7-flash"


class GLMConfig(BaseModel):
    """仅保存调用 GLM 所需配置，并在日志表示中隐藏密钥。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    api_key: SecretStr
    model: str = Field(default=DEFAULT_GLM_MODEL, min_length=1, max_length=100)
    base_url: str = DEFAULT_GLM_BASE_URL
    timeout_seconds: float = Field(default=30.0, gt=0, le=120)
    max_tokens: int = Field(default=2_000, ge=128, le=32_768)
    temperature: float = Field(default=0.1, gt=0, le=1)

    def model_post_init(self, __context: Any) -> None:
        """尽早拒绝空密钥和不安全的远程地址。"""

        if not self.api_key.get_secret_value().strip():
            raise ValueError("api_key 不能为空")
        parsed = urlparse(self.base_url)
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValueError("base_url 必须是有效的 HTTPS 地址")

    @property
    def endpoint(self) -> str:
        """返回规范化后的对话补全地址。"""

        base = self.base_url.rstrip("/")
        if base.endswith("/chat/completions"):
            return base
        return f"{base}/chat/completions"

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "GLMConfig":
        """只从环境变量读取密钥，避免配置文件误提交。"""

        source = os.environ if environ is None else environ
        api_key = source.get("ZHIPUAI_API_KEY", "").strip()
        if not api_key:
            raise LLMConfigurationError(
                "缺少环境变量 ZHIPUAI_API_KEY；请使用已轮换的新密钥"
            )
        try:
            return cls(
                api_key=SecretStr(api_key),
                model=source.get("GLM_MODEL", DEFAULT_GLM_MODEL),
                base_url=source.get("GLM_BASE_URL", DEFAULT_GLM_BASE_URL),
            )
        except ValidationError as exc:
            raise LLMConfigurationError(
                f"GLM 环境配置不合法：{exc.error_count()} 个错误"
            ) from exc


class _GLMMessage(BaseModel):
    """GLM 响应中的消息片段。"""

    content: str


class _GLMChoice(BaseModel):
    """GLM 响应中的候选结果。"""

    message: _GLMMessage
    finish_reason: str | None = None


class _GLMResponse(BaseModel):
    """本模块真正依赖的最小响应结构。"""

    id: str | None = None
    model: str | None = None
    choices: tuple[_GLMChoice, ...] = Field(min_length=1)


def _safe_error_message(response: httpx.Response, api_key: str) -> str:
    """提取有限错误信息，并兜底移除可能被回显的密钥。"""

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


class GLMChatClient:
    """使用连接复用调用 GLM JSON 模式，并统一翻译错误。"""

    def __init__(
        self,
        config: GLMConfig,
        *,
        http_client: httpx.Client | None = None,
    ) -> None:
        self.config = config
        self._owns_client = http_client is None
        self._client = http_client or httpx.Client(timeout=config.timeout_seconds)

    def close(self) -> None:
        """只关闭由适配器自己创建的连接池。"""

        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "GLMChatClient":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def generate_json(self, request: StructuredJSONRequest) -> Mapping[str, Any]:
        """调用非流式 JSON 模式，并把响应解析为普通映射。"""

        api_key = self.config.api_key.get_secret_value()
        body = {
            "model": self.config.model,
            "messages": [message.model_dump() for message in request.messages],
            "response_format": {"type": "json_object"},
            "thinking": {"type": "disabled"},
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
            raise LLMTimeoutError("GLM 请求超时") from exc
        except httpx.RequestError as exc:
            raise LLMTransportError(
                f"GLM 网络请求失败：{type(exc).__name__}"
            ) from exc

        if response.status_code in {401, 403}:
            raise LLMAuthenticationError(
                f"GLM 鉴权失败（HTTP {response.status_code}）："
                f"{_safe_error_message(response, api_key)}"
            )
        if response.status_code == 429:
            raise LLMRateLimitError(
                "GLM 请求受到限流、配额或余额限制："
                f"{_safe_error_message(response, api_key)}"
            )
        if response.is_error:
            raise LLMResponseError(
                f"GLM 返回 HTTP {response.status_code}："
                f"{_safe_error_message(response, api_key)}"
            )

        try:
            envelope = _GLMResponse.model_validate(response.json())
        except (ValueError, ValidationError) as exc:
            raise LLMResponseError("GLM 返回的协议结构不完整") from exc

        content = envelope.choices[0].message.content
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError as exc:
            raise LLMStructuredOutputError("GLM 未返回合法 JSON") from exc
        if not isinstance(parsed, dict):
            raise LLMStructuredOutputError("GLM JSON 顶层必须是对象")
        return parsed
