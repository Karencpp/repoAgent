"""RepoAgent 的代码库检索增强模块。"""

from .chunking import (
    RepositoryChunker,
    RepositoryChunkerConfig,
    RepositoryScan,
    RepositorySourceFile,
)
from .embeddings import (
    DEFAULT_GLM_EMBEDDING_DIMENSIONS,
    DEFAULT_GLM_EMBEDDING_MODEL,
    EmbeddingClient,
    FeatureHashEmbeddingClient,
    GLMEmbeddingClient,
    GLMEmbeddingConfig,
)
from .evaluation import evaluate_retrieval
from .index import (
    HybridSearchConfig,
    RAGEmbeddingMismatchError,
    RAGIndexError,
    RAGIndexNotReadyError,
    RAGRevisionMismatchError,
    SQLiteRAGIndex,
)
from .models import (
    IndexingReport,
    RepositoryChunkDraft,
    RetrievalCase,
    RetrievalCaseScore,
    RetrievalEvaluationReport,
    RetrievalHit,
    RetrievalResult,
)
from .ports import RAGIndexPort
from .postgres import PostgresRAGError, PostgresRAGIndex
from .rerank import (
    DEFAULT_GLM_RERANK_MODEL,
    GLMRerankerClient,
    GLMRerankerConfig,
    RerankerClient,
    RerankResponse,
    RerankScore,
)
from .tool import (
    RAG_TOOL_DEFINITION,
    RepositoryRAGTool,
    SearchRepositoryKnowledgeArguments,
    register_repository_rag_tool,
)

__all__ = [
    "DEFAULT_GLM_EMBEDDING_DIMENSIONS",
    "DEFAULT_GLM_EMBEDDING_MODEL",
    "DEFAULT_GLM_RERANK_MODEL",
    "EmbeddingClient",
    "FeatureHashEmbeddingClient",
    "GLMEmbeddingClient",
    "GLMEmbeddingConfig",
    "GLMRerankerClient",
    "GLMRerankerConfig",
    "HybridSearchConfig",
    "IndexingReport",
    "RAGEmbeddingMismatchError",
    "RAGIndexError",
    "RAGIndexNotReadyError",
    "RAGRevisionMismatchError",
    "RAGIndexPort",
    "PostgresRAGError",
    "PostgresRAGIndex",
    "RAG_TOOL_DEFINITION",
    "RepositoryChunkDraft",
    "RepositoryChunker",
    "RepositoryChunkerConfig",
    "RepositoryRAGTool",
    "RepositoryScan",
    "RepositorySourceFile",
    "RetrievalCase",
    "RetrievalCaseScore",
    "RetrievalEvaluationReport",
    "RetrievalHit",
    "RetrievalResult",
    "RerankerClient",
    "RerankResponse",
    "RerankScore",
    "SQLiteRAGIndex",
    "SearchRepositoryKnowledgeArguments",
    "evaluate_retrieval",
    "register_repository_rag_tool",
]
