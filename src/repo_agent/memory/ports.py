"""长期记忆存储后端端口。"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from repo_agent.projects import ProjectContext

from .models import (
    MemoryMaintenanceReport,
    MemoryRecord,
    MemorySearchRequest,
    MemorySearchResult,
    MemoryWrite,
)


class MemoryStorePort(Protocol):
    """屏蔽 SQLite、PostgreSQL 或其他记忆后端的统一端口。"""

    def put(self, context: ProjectContext, memory: MemoryWrite) -> MemoryRecord:
        """新增一条记忆。"""

    def put_with_key(
        self,
        context: ProjectContext,
        memory: MemoryWrite,
        *,
        memory_key: str | None,
    ) -> MemoryRecord:
        """新增一条带稳定事实键的记忆。"""

    def replace(
        self,
        context: ProjectContext,
        old_memory_id: str,
        replacement: MemoryWrite,
    ) -> MemoryRecord:
        """用新记忆替换旧活动记忆。"""

    def search(
        self,
        context: ProjectContext,
        request: MemorySearchRequest,
    ) -> MemorySearchResult:
        """按项目和元数据过滤后检索记忆。"""

    def get(self, context: ProjectContext, memory_id: str) -> MemoryRecord:
        """读取当前项目内的一条记忆。"""

    def forget(
        self,
        context: ProjectContext,
        memory_id: str,
        *,
        actor: str = "host",
        reason: str = "显式遗忘请求",
    ) -> None:
        """遗忘一条记忆并保留墓碑审计。"""

    def expire(
        self,
        context: ProjectContext,
        *,
        now: datetime | None = None,
    ) -> MemoryMaintenanceReport:
        """执行 TTL 清理。"""

    def reembed_project(self, context: ProjectContext) -> MemoryMaintenanceReport:
        """重建一个项目的记忆向量空间。"""

    def close(self) -> None:
        """释放后端连接。"""
