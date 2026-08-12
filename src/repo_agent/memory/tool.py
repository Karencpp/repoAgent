"""受 Tool Registry 管理的只读长期记忆检索工具。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from repo_agent.projects import ProjectContext
from repo_agent.tools.catalog import ToolDefinition
from repo_agent.tools.models import ToolErrorKind, ToolResult
from repo_agent.tools.registry import ToolRegistry

from .models import MemorySearchRequest, MemorySearchResult
from .store import MemoryStoreError, SQLiteMemoryStore


MEMORY_SEARCH_TOOL_DEFINITION = ToolDefinition(
    name="search_project_memory",
    description=(
        "检索当前项目跨任务保存的已验证经历、语义事实和感知记录，"
        "默认排除其他代码版本的 revision 级记忆。"
    ),
    access="read",
    executes_project_code=False,
    requires_explicit_authorization=False,
)


class SearchProjectMemoryArguments(BaseModel):
    """模型可提交的长期记忆搜索参数。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    query: str = Field(min_length=1, max_length=2_000)
    memory_types: tuple[Literal["episodic", "semantic", "perceptual"], ...] = (
        "episodic",
        "semantic",
        "perceptual",
    )
    min_importance: float = Field(default=0.0, ge=0, le=1)
    top_k: int = Field(default=5, ge=1, le=20)


class ProjectMemorySearchTool:
    """把固定项目上下文绑定到长期记忆 Store。"""

    def __init__(self, store: SQLiteMemoryStore, context: ProjectContext) -> None:
        self.store = store
        self.context = context

    def search(
        self,
        arguments: SearchProjectMemoryArguments,
    ) -> ToolResult[MemorySearchResult]:
        """只读取 verified 活动记忆，不允许模型直接写入事实。"""

        try:
            result = self.store.search(
                self.context,
                MemorySearchRequest(
                    query=arguments.query,
                    memory_types=arguments.memory_types,
                    claim_statuses=("verified",),
                    min_importance=arguments.min_importance,
                    top_k=arguments.top_k,
                ),
            )
            return ToolResult.success(
                result,
                metadata={
                    "memory_ids": [hit.record.memory_id for hit in result.hits],
                    "repo_revision": result.repo_revision,
                },
            )
        except (ValueError, MemoryStoreError) as exc:
            return ToolResult.failure(
                ToolErrorKind.INVALID_ARGUMENT,
                f"长期记忆检索失败：{exc}",
            )


def register_project_memory_search_tool(
    registry: ToolRegistry,
    store: SQLiteMemoryStore,
    context: ProjectContext,
) -> ProjectMemorySearchTool:
    """把只读 Memory Tool 注册到当前项目工具集合。"""

    tool = ProjectMemorySearchTool(store, context)
    registry.register(
        MEMORY_SEARCH_TOOL_DEFINITION,
        SearchProjectMemoryArguments,
        tool.search,
    )
    return tool
