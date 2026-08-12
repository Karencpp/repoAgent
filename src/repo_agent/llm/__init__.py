"""RepoAgent 的结构化大模型适配层。"""

from .adapters import (
    StructuredAdapterConfig,
    StructuredDecisionClient,
    StructuredPlanner,
    StructuredReflector,
)
from .contracts import (
    ChatMessage,
    LLMAuthenticationError,
    LLMConfigurationError,
    LLMProviderError,
    LLMRateLimitError,
    LLMResponseError,
    LLMStructuredOutputError,
    LLMTimeoutError,
    LLMTransportError,
    StructuredJSONClient,
    StructuredJSONRequest,
)
from .glm import (
    DEFAULT_GLM_BASE_URL,
    DEFAULT_GLM_MODEL,
    GLMChatClient,
    GLMConfig,
)
from .deepseek import (
    DEFAULT_DEEPSEEK_BASE_URL,
    DEFAULT_DEEPSEEK_MODEL,
    DeepSeekChatClient,
    DeepSeekConfig,
)
from .providers import (
    LLMProviderName,
    resolve_llm_provider,
    structured_client_from_env,
)

__all__ = [
    "ChatMessage",
    "DEFAULT_DEEPSEEK_BASE_URL",
    "DEFAULT_DEEPSEEK_MODEL",
    "DEFAULT_GLM_BASE_URL",
    "DEFAULT_GLM_MODEL",
    "GLMChatClient",
    "GLMConfig",
    "DeepSeekChatClient",
    "DeepSeekConfig",
    "LLMAuthenticationError",
    "LLMConfigurationError",
    "LLMProviderError",
    "LLMRateLimitError",
    "LLMResponseError",
    "LLMStructuredOutputError",
    "LLMTimeoutError",
    "LLMTransportError",
    "LLMProviderName",
    "StructuredAdapterConfig",
    "StructuredDecisionClient",
    "StructuredJSONClient",
    "StructuredJSONRequest",
    "StructuredPlanner",
    "StructuredReflector",
    "resolve_llm_provider",
    "structured_client_from_env",
]
