"""代码库检索索引的基础设施端口。"""

from __future__ import annotations

from typing import Literal, Protocol

from repo_agent.projects import ProjectContext

from .models import IndexingReport, RetrievalResult


class RAGIndexPort(Protocol):
    """屏蔽 SQLite、pgvector 或其他索引后端的统一端口。"""

    def index_repository(self, context: ProjectContext) -> IndexingReport:
        """增量索引一个显式项目。"""

    def search(
        self,
        context: ProjectContext,
        query: str,
        *,
        top_k: int = 5,
        mode: Literal["hybrid", "lexical", "dense"] = "hybrid",
    ) -> RetrievalResult:
        """执行代码库检索，并返回带引用的结果。"""

    def delete_project(self, project_id: str) -> None:
        """删除一个项目的全部索引数据。"""

    def close(self) -> None:
        """释放后端连接。"""
