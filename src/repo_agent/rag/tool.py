"""把代码库混合检索暴露为受 Tool Registry 管理的只读工具。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from repo_agent.projects import ProjectContext
from repo_agent.tools.catalog import ToolDefinition
from repo_agent.tools.models import ToolErrorKind, ToolResult
from repo_agent.tools.registry import ToolRegistry

from .index import RAGIndexError, SQLiteRAGIndex
from .models import RetrievalResult


RAG_TOOL_DEFINITION = ToolDefinition(
    name="search_repository_knowledge",
    description=(
        "在当前项目和代码版本的增量索引中执行关键词与向量混合检索，"
        "返回带路径和行号的候选证据。"
    ),
    access="read",
    executes_project_code=False,
    requires_explicit_authorization=False,
)


class SearchRepositoryKnowledgeArguments(BaseModel):
    """模型可提交的代码库知识检索参数。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    query: str = Field(min_length=1, max_length=2_000)
    top_k: int = Field(default=5, ge=1, le=20)
    mode: Literal["hybrid", "lexical", "dense"] = "hybrid"


class RepositoryRAGTool:
    """将固定 ProjectContext 绑定到检索服务。"""

    def __init__(self, index: SQLiteRAGIndex, context: ProjectContext) -> None:
        self.index = index
        self.context = context

    def search(
        self,
        arguments: SearchRepositoryKnowledgeArguments,
    ) -> ToolResult[RetrievalResult]:
        """执行只读检索，并把索引状态问题转为工具错误。"""

        try:
            result = self.index.search(
                self.context,
                arguments.query,
                top_k=arguments.top_k,
                mode=arguments.mode,
            )
            return ToolResult.success(
                result,
                metadata={
                    "citations": [hit.citation for hit in result.hits],
                    "repo_revision": result.repo_revision,
                },
            )
        except (ValueError, RAGIndexError) as exc:
            return ToolResult.failure(
                ToolErrorKind.INVALID_ARGUMENT,
                f"代码库知识检索失败：{exc}",
            )


def register_repository_rag_tool(
    registry: ToolRegistry,
    index: SQLiteRAGIndex,
    context: ProjectContext,
) -> RepositoryRAGTool:
    """把 RAG 工具追加到已有项目工具注册表。"""

    tool = RepositoryRAGTool(index, context)
    registry.register(
        RAG_TOOL_DEFINITION,
        SearchRepositoryKnowledgeArguments,
        tool.search,
    )
    return tool
