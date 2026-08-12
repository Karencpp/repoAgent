"""Provider-neutral reranking contract and the GLM Rerank HTTP adapter."""

from __future__ import annotations

import os
import time
from typing import Any, Mapping, Protocol, Sequence
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError

from repo_agent.llm.contracts import (
    LLMAuthenticationError,
    LLMConfigurationError,
    LLMRateLimitError,
    LLMResponseError,
    LLMTimeoutError,
    LLMTransportError,
)
from repo_agent.llm.glm import DEFAULT_GLM_BASE_URL


DEFAULT_GLM_RERANK_MODEL = "rerank"


class RerankScore(BaseModel):
    """One provider score mapped back to the original document index."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    index: int = Field(ge=0)
    relevance_score: float


class RerankResponse(BaseModel):
    """Stable result used by benchmarks and retrieval backends."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    scores: tuple[RerankScore, ...]
    prompt_tokens: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)
    request_id: str | None = None


class RerankerClient(Protocol):
    @property
    def model_id(self) -> str: ...

    def rerank(self, query: str, documents: Sequence[str]) -> RerankResponse: ...


class GLMRerankerConfig(BaseModel):
    """Validated configuration for the official GLM rerank endpoint."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    api_key: SecretStr
    model: str = Field(default=DEFAULT_GLM_RERANK_MODEL, min_length=1, max_length=100)
    base_url: str = DEFAULT_GLM_BASE_URL
    timeout_seconds: float = Field(default=60.0, gt=0, le=180)
    max_documents: int = Field(default=128, ge=1, le=128)
    max_query_chars: int = Field(default=4096, ge=1, le=4096)
    max_document_chars: int = Field(default=4096, ge=1, le=4096)
    max_pair_chars: int = Field(default=3500, ge=2, le=8192)
    max_retries: int = Field(default=3, ge=0, le=10)
    retry_backoff_seconds: float = Field(default=1.0, ge=0, le=30)
    allow_external_data: bool = False

    def model_post_init(self, __context: Any) -> None:
        if not self.api_key.get_secret_value().strip():
            raise ValueError("api_key 不能为空")
        parsed = urlparse(self.base_url)
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValueError("base_url 必须是有效的 HTTPS 地址")

    @property
    def endpoint(self) -> str:
        base = self.base_url.rstrip("/")
        return base if base.endswith("/rerank") else f"{base}/rerank"

    @classmethod
    def from_env(
        cls,
        environ: Mapping[str, str] | None = None,
    ) -> "GLMRerankerConfig":
        source = os.environ if environ is None else environ
        api_key = source.get("ZHIPUAI_API_KEY", "").strip()
        if not api_key:
            raise LLMConfigurationError("缺少环境变量 ZHIPUAI_API_KEY")
        try:
            return cls(
                api_key=SecretStr(api_key),
                model=source.get("GLM_RERANK_MODEL", DEFAULT_GLM_RERANK_MODEL),
                base_url=source.get("GLM_BASE_URL", DEFAULT_GLM_BASE_URL),
                allow_external_data=(
                    source.get("ALLOW_EXTERNAL_CODE_RERANKING", "").casefold()
                    == "true"
                ),
            )
        except ValidationError as exc:
            raise LLMConfigurationError(
                f"GLM Rerank 配置不合法：{exc.error_count()} 个错误"
            ) from exc


class _ProviderResult(BaseModel):
    index: int = Field(ge=0)
    relevance_score: float


class _ProviderUsage(BaseModel):
    prompt_tokens: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)


class _ProviderResponse(BaseModel):
    results: tuple[_ProviderResult, ...]
    usage: _ProviderUsage = Field(default_factory=_ProviderUsage)
    request_id: str | None = None


def _safe_error_message(response: httpx.Response, api_key: str) -> str:
    message = "provider did not return an error message"
    try:
        payload = response.json()
        if isinstance(payload, Mapping):
            error = payload.get("error")
            if isinstance(error, Mapping):
                message = str(error.get("message") or error.get("msg") or message)
            else:
                message = str(payload.get("message") or payload.get("msg") or message)
    except (TypeError, ValueError):
        if response.text.strip():
            message = response.text.strip()
    return message.replace(api_key, "***")[:500]


class GLMRerankerClient:
    """Call GLM Rerank with bounded retries and strict index validation."""

    def __init__(
        self,
        config: GLMRerankerConfig,
        *,
        http_client: httpx.Client | None = None,
    ) -> None:
        self.config = config
        self._owns_client = http_client is None
        self._client = http_client or httpx.Client(timeout=config.timeout_seconds)

    @property
    def model_id(self) -> str:
        return f"glm:{self.config.model}"

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def rerank(self, query: str, documents: Sequence[str]) -> RerankResponse:
        normalized_query = query.strip()
        normalized_documents = tuple(documents)
        if not self.config.allow_external_data:
            raise LLMConfigurationError(
                "GLM Rerank 会把候选文本发送到外部服务，当前未获得外部数据授权"
            )
        if not normalized_query or len(normalized_query) > self.config.max_query_chars:
            raise ValueError("Rerank query 为空或超过字符上限")
        if not normalized_documents or len(normalized_documents) > self.config.max_documents:
            raise ValueError("Rerank documents 数量超出范围")
        if any(
            not document.strip()
            or len(document) > self.config.max_document_chars
            for document in normalized_documents
        ):
            raise ValueError("Rerank document 为空或超过字符上限")
        if any(
            len(normalized_query) + len(document) > self.config.max_pair_chars
            for document in normalized_documents
        ):
            raise ValueError("Rerank query 与 document 合计超过字符上限")

        api_key = self.config.api_key.get_secret_value()
        response: httpx.Response | None = None
        last_error: httpx.RequestError | None = None
        for attempt in range(self.config.max_retries + 1):
            try:
                response = self._client.post(
                    self.config.endpoint,
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": self.config.model,
                        "query": normalized_query,
                        "documents": list(normalized_documents),
                        "top_n": len(normalized_documents),
                        "return_documents": False,
                        "return_raw_scores": False,
                    },
                    timeout=self.config.timeout_seconds,
                )
                last_error = None
            except httpx.RequestError as exc:
                response = None
                last_error = exc

            retryable_status = response is not None and (
                response.status_code == 429 or response.status_code >= 500
            )
            if not (retryable_status or last_error is not None):
                break
            if attempt >= self.config.max_retries:
                break
            delay = self.config.retry_backoff_seconds * (2**attempt)
            if response is not None:
                try:
                    delay = max(delay, float(response.headers.get("Retry-After", "")))
                except ValueError:
                    pass
            time.sleep(min(delay, 30.0))

        if last_error is not None:
            if isinstance(last_error, httpx.TimeoutException):
                raise LLMTimeoutError("GLM Rerank 请求超时") from last_error
            raise LLMTransportError(
                f"GLM Rerank 网络请求失败：{type(last_error).__name__}"
            ) from last_error
        if response is None:
            raise LLMTransportError("GLM Rerank 网络请求未返回响应")
        if response.status_code in {401, 403}:
            raise LLMAuthenticationError(
                f"GLM Rerank 鉴权失败（HTTP {response.status_code}）："
                f"{_safe_error_message(response, api_key)}"
            )
        if response.status_code == 429:
            raise LLMRateLimitError(
                "GLM Rerank 请求受到限流、配额或余额限制："
                f"{_safe_error_message(response, api_key)}"
            )
        if response.is_error:
            raise LLMResponseError(
                f"GLM Rerank 返回 HTTP {response.status_code}："
                f"{_safe_error_message(response, api_key)}"
            )
        try:
            envelope = _ProviderResponse.model_validate(response.json())
        except (ValueError, ValidationError) as exc:
            raise LLMResponseError("GLM Rerank 返回的协议结构不完整") from exc

        ordered = tuple(
            sorted(envelope.results, key=lambda item: item.relevance_score, reverse=True)
        )
        indexes = [item.index for item in ordered]
        if len(indexes) != len(normalized_documents) or set(indexes) != set(
            range(len(normalized_documents))
        ):
            raise LLMResponseError("GLM Rerank 未返回完整且唯一的候选索引")
        return RerankResponse(
            scores=tuple(
                RerankScore(index=item.index, relevance_score=item.relevance_score)
                for item in ordered
            ),
            prompt_tokens=envelope.usage.prompt_tokens,
            total_tokens=envelope.usage.total_tokens,
            request_id=envelope.request_id,
        )
