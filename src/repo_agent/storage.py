"""存储后端配置、工厂和迁移入口。"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from typing import Literal

from repo_agent.memory import MemoryStorePort, SQLiteMemoryStore
from repo_agent.rag import RAGIndexPort, SQLiteRAGIndex
from repo_agent.rag.embeddings import EmbeddingClient


StorageBackend = Literal["sqlite", "postgres"]


def _redact_dsn(value: str) -> str:
    """在错误信息中移除可能出现的数据库密码。"""

    if "://" not in value or "@" not in value:
        return value
    scheme, rest = value.split("://", 1)
    credentials, host = rest.split("@", 1)
    user = credentials.split(":", 1)[0]
    return f"{scheme}://{user}:***@{host}"


@dataclass(frozen=True, slots=True)
class StorageConfig:
    """RepoAgent 持久化后端选择。"""

    backend: StorageBackend = "sqlite"
    sqlite_state_dir: Path | str | None = None
    postgres_dsn: str | None = None
    postgres_schema_version: str = "0001"

    @classmethod
    def from_env(
        cls,
        *,
        default_state_dir: Path,
        backend: StorageBackend | None = None,
    ) -> "StorageConfig":
        """按 CLI > 环境变量 > 默认值的顺序解析存储配置。"""

        selected = backend or os.environ.get("REPO_AGENT_STORAGE_BACKEND", "sqlite")
        if selected not in {"sqlite", "postgres"}:
            raise ValueError("REPO_AGENT_STORAGE_BACKEND 必须是 sqlite 或 postgres")
        return cls(
            backend=selected,
            sqlite_state_dir=default_state_dir,
            postgres_dsn=os.environ.get("REPO_AGENT_POSTGRES_DSN"),
        )

    def require_postgres_dsn(self) -> str:
        """返回 PostgreSQL DSN，缺失时早失败且不泄露密码。"""

        if not self.postgres_dsn:
            raise ValueError("PostgreSQL 后端需要设置 REPO_AGENT_POSTGRES_DSN 或配置 DSN")
        return self.postgres_dsn


class InfrastructureFactory:
    """集中创建 RAG、Memory 和 Checkpoint 后端实例。"""

    def __init__(
        self,
        storage: StorageConfig,
        *,
        embedding_client: EmbeddingClient,
    ) -> None:
        self.storage = storage
        self.embedding_client = embedding_client

    def create_rag_index(self) -> RAGIndexPort:
        """创建代码库索引后端。"""

        if self.storage.backend == "sqlite":
            state_dir = Path(self.storage.sqlite_state_dir or ".repo-agent").resolve()
            return SQLiteRAGIndex(state_dir / "rag.sqlite3", self.embedding_client)
        from repo_agent.rag.postgres import PostgresRAGIndex

        return PostgresRAGIndex(
            self.storage.require_postgres_dsn(),
            self.embedding_client,
        )

    def create_memory_store(self) -> MemoryStorePort:
        """创建长期记忆后端。"""

        if self.storage.backend == "sqlite":
            state_dir = Path(self.storage.sqlite_state_dir or ".repo-agent").resolve()
            return SQLiteMemoryStore(
                state_dir / "memory.sqlite3",
                self.embedding_client,
            )
        from repo_agent.memory.postgres import PostgresMemoryStore

        return PostgresMemoryStore(
            self.storage.require_postgres_dsn(),
            self.embedding_client,
        )


class StorageConfigurationError(RuntimeError):
    """存储后端配置不可用。"""


__all__ = [
    "InfrastructureFactory",
    "StorageBackend",
    "StorageConfig",
    "StorageConfigurationError",
    "_redact_dsn",
]
