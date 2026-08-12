"""代码库 RAG 使用的向量化端口与 GLM 实现。"""

from __future__ import annotations

import hashlib
import math
import os
import re
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


DEFAULT_GLM_EMBEDDING_MODEL = "embedding-3"
DEFAULT_GLM_EMBEDDING_DIMENSIONS = 512
_WORD_PATTERN = re.compile(
    r"[A-Za-z_][A-Za-z0-9_]*|\d+|[\u3400-\u4dbf\u4e00-\u9fff]+"
)
_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


class EmbeddingClient(Protocol):
    """索引层依赖的供应商无关向量化端口。"""

    @property
    def model_id(self) -> str:
        """返回可以识别向量语义空间的稳定名称。"""

    @property
    def dimensions(self) -> int:
        """返回输出向量维度。"""

    def embed_texts(
        self,
        texts: Sequence[str],
    ) -> tuple[tuple[float, ...], ...]:
        """按输入顺序返回等量向量。"""


def _features(text: str) -> tuple[str, ...]:
    """提取适合代码标识符和中英文文本的稳定特征。"""

    expanded = _CAMEL_BOUNDARY.sub(" ", text)
    tokens: list[str] = []
    raw_tokens = [match.group(0) for match in _WORD_PATTERN.finditer(text)]
    split_tokens = [match.group(0) for match in _WORD_PATTERN.finditer(expanded)]
    for raw_token in [*raw_tokens, *split_tokens]:
        token = raw_token.casefold()
        tokens.append(token)
        if any("\u3400" <= char <= "\u9fff" for char in token):
            tokens.extend(char for char in token if char.strip())
            tokens.extend(
                token[index : index + 2]
                for index in range(max(0, len(token) - 1))
            )
    return tuple(tokens)


class FeatureHashEmbeddingClient:
    """无需网络的确定性特征哈希向量，专用于测试和降级检索。"""

    def __init__(self, dimensions: int = 256) -> None:
        if dimensions < 32:
            raise ValueError("dimensions 必须大于等于 32")
        self._dimensions = dimensions

    @property
    def model_id(self) -> str:
        return f"local-feature-hash-v1-{self.dimensions}"

    @property
    def dimensions(self) -> int:
        return self._dimensions

    def embed_texts(
        self,
        texts: Sequence[str],
    ) -> tuple[tuple[float, ...], ...]:
        """把词元映射到固定维度，并执行 L2 归一化。"""

        vectors: list[tuple[float, ...]] = []
        for text in texts:
            values = [0.0] * self.dimensions
            for feature in _features(text):
                digest = hashlib.blake2b(
                    feature.encode("utf-8"),
                    digest_size=16,
                ).digest()
                index = int.from_bytes(digest[:8], "big") % self.dimensions
                sign = 1.0 if digest[8] & 1 else -1.0
                values[index] += sign
            norm = math.sqrt(sum(value * value for value in values))
            if norm:
                values = [value / norm for value in values]
            vectors.append(tuple(values))
        return tuple(vectors)


class GLMEmbeddingConfig(BaseModel):
    """GLM Embedding 的安全配置。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    api_key: SecretStr
    model: str = Field(
        default=DEFAULT_GLM_EMBEDDING_MODEL,
        min_length=1,
        max_length=100,
    )
    dimensions: int = Field(
        default=DEFAULT_GLM_EMBEDDING_DIMENSIONS,
    )
    base_url: str = DEFAULT_GLM_BASE_URL
    timeout_seconds: float = Field(default=30.0, gt=0, le=120)
    batch_size: int = Field(default=32, ge=1, le=64)
    max_text_chars: int = Field(default=12_000, ge=500, le=30_000)
    max_retries: int = Field(default=3, ge=0, le=10)
    retry_backoff_seconds: float = Field(default=1.0, ge=0, le=30)
    allow_external_data: bool = False

    def model_post_init(self, __context: Any) -> None:
        """检查官方支持的维度和 HTTPS 地址。"""

        if not self.api_key.get_secret_value().strip():
            raise ValueError("api_key 不能为空")
        if self.dimensions not in {256, 512, 1024, 2048}:
            raise ValueError("dimensions 必须是 256、512、1024 或 2048")
        parsed = urlparse(self.base_url)
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValueError("base_url 必须是有效的 HTTPS 地址")

    @property
    def endpoint(self) -> str:
        """返回规范化后的 Embedding 地址。"""

        base = self.base_url.rstrip("/")
        if base.endswith("/embeddings"):
            return base
        return f"{base}/embeddings"

    @classmethod
    def from_env(
        cls,
        environ: Mapping[str, str] | None = None,
    ) -> "GLMEmbeddingConfig":
        """从环境变量读取密钥、模型和维度。"""

        source = os.environ if environ is None else environ
        api_key = source.get("ZHIPUAI_API_KEY", "").strip()
        if not api_key:
            raise LLMConfigurationError(
                "缺少环境变量 ZHIPUAI_API_KEY；请使用已轮换的新密钥"
            )
        raw_dimensions = source.get(
            "GLM_EMBEDDING_DIMENSIONS",
            str(DEFAULT_GLM_EMBEDDING_DIMENSIONS),
        )
        try:
            dimensions = int(raw_dimensions)
        except ValueError as exc:
            raise LLMConfigurationError(
                "GLM_EMBEDDING_DIMENSIONS 必须是整数"
            ) from exc
        try:
            return cls(
                api_key=SecretStr(api_key),
                model=source.get(
                    "GLM_EMBEDDING_MODEL",
                    DEFAULT_GLM_EMBEDDING_MODEL,
                ),
                dimensions=dimensions,
                base_url=source.get("GLM_BASE_URL", DEFAULT_GLM_BASE_URL),
                allow_external_data=(
                    source.get("ALLOW_EXTERNAL_CODE_EMBEDDING", "").casefold()
                    == "true"
                ),
            )
        except ValidationError as exc:
            raise LLMConfigurationError(
                f"GLM Embedding 配置不合法：{exc.error_count()} 个错误"
            ) from exc


class _EmbeddingItem(BaseModel):
    """供应商返回的一条向量。"""

    index: int = Field(ge=0)
    embedding: tuple[float, ...] = Field(min_length=1)


class _EmbeddingResponse(BaseModel):
    """本模块依赖的最小 Embedding 响应。"""

    model: str
    data: tuple[_EmbeddingItem, ...]


def _safe_error_message(response: httpx.Response, api_key: str) -> str:
    """提取有限错误说明并移除可能回显的密钥。"""

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


class GLMEmbeddingClient:
    """批量调用 GLM Embedding，并保证输入输出顺序和维度。"""

    def __init__(
        self,
        config: GLMEmbeddingConfig,
        *,
        http_client: httpx.Client | None = None,
    ) -> None:
        self.config = config
        self._owns_client = http_client is None
        self._client = http_client or httpx.Client(timeout=config.timeout_seconds)

    @property
    def model_id(self) -> str:
        return f"glm:{self.config.model}:{self.config.dimensions}"

    @property
    def dimensions(self) -> int:
        return self.config.dimensions

    def close(self) -> None:
        """只关闭适配器自己创建的连接池。"""

        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "GLMEmbeddingClient":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def embed_texts(
        self,
        texts: Sequence[str],
    ) -> tuple[tuple[float, ...], ...]:
        """按官方批量上限分组调用，并保持原始顺序。"""

        normalized = tuple(texts)
        if not normalized:
            return ()
        if not self.config.allow_external_data:
            raise LLMConfigurationError(
                "GLM Embedding 会把文本发送到外部服务，当前未获得外部数据授权"
            )
        for text in normalized:
            if not text.strip():
                raise ValueError("Embedding 输入不能为空")
            if len(text) > self.config.max_text_chars:
                raise ValueError(
                    f"Embedding 输入超过字符上限：{len(text)}"
                )

        vectors: list[tuple[float, ...]] = []
        for offset in range(0, len(normalized), self.config.batch_size):
            batch = normalized[offset : offset + self.config.batch_size]
            vectors.extend(self._embed_batch(batch))
        return tuple(vectors)

    def _embed_batch(
        self,
        texts: Sequence[str],
    ) -> tuple[tuple[float, ...], ...]:
        """执行一次真实 HTTP 请求并校验结果数量、索引和维度。"""

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
                        "input": list(texts),
                        "dimensions": self.config.dimensions,
                    },
                    timeout=self.config.timeout_seconds,
                )
                last_error = None
            except httpx.RequestError as exc:
                last_error = exc
                response = None

            retryable_status = response is not None and (
                response.status_code == 429 or response.status_code >= 500
            )
            retryable_error = last_error is not None
            if not (retryable_status or retryable_error):
                break
            if attempt >= self.config.max_retries:
                break

            delay = self.config.retry_backoff_seconds * (2**attempt)
            if response is not None:
                retry_after = response.headers.get("Retry-After", "").strip()
                try:
                    delay = max(delay, float(retry_after))
                except ValueError:
                    pass
            time.sleep(min(delay, 30.0))

        if last_error is not None:
            if isinstance(last_error, httpx.TimeoutException):
                raise LLMTimeoutError("GLM Embedding 请求超时") from last_error
            raise LLMTransportError(
                f"GLM Embedding 网络请求失败：{type(last_error).__name__}"
            ) from last_error
        if response is None:
            raise LLMTransportError("GLM Embedding 网络请求未返回响应")

        if response.status_code in {401, 403}:
            raise LLMAuthenticationError(
                f"GLM Embedding 鉴权失败（HTTP {response.status_code}）："
                f"{_safe_error_message(response, api_key)}"
            )
        if response.status_code == 429:
            raise LLMRateLimitError(
                "GLM Embedding 请求受到限流、配额或余额限制："
                f"{_safe_error_message(response, api_key)}"
            )
        if response.is_error:
            raise LLMResponseError(
                f"GLM Embedding 返回 HTTP {response.status_code}："
                f"{_safe_error_message(response, api_key)}"
            )

        try:
            envelope = _EmbeddingResponse.model_validate(response.json())
        except (ValueError, ValidationError) as exc:
            raise LLMResponseError("GLM Embedding 返回的协议结构不完整") from exc
        ordered = sorted(envelope.data, key=lambda item: item.index)
        if [item.index for item in ordered] != list(range(len(texts))):
            raise LLMResponseError("GLM Embedding 返回的向量索引不连续")
        if any(len(item.embedding) != self.dimensions for item in ordered):
            raise LLMResponseError("GLM Embedding 返回的向量维度不匹配")
        return tuple(item.embedding for item in ordered)
