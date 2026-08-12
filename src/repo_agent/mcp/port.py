"""MCP 传输和 SDK 适配器需要实现的稳定端口。"""

from __future__ import annotations

from typing import Any, Mapping, Protocol

from .models import (
    MCPDiscoveryResult,
    MCPRequestContext,
    MCPToolListPage,
    MCPToolResult,
)


class MCPClientPort(Protocol):
    """屏蔽 stdio、Streamable HTTP 和具体 SDK 版本差异。"""

    def discover(self, context: MCPRequestContext) -> MCPDiscoveryResult:
        """发现 Server 支持的协议版本、身份和能力。"""

    def list_tools(
        self,
        context: MCPRequestContext,
        *,
        cursor: str | None = None,
    ) -> MCPToolListPage:
        """读取一页远程工具目录。"""

    def call_tool(
        self,
        context: MCPRequestContext,
        *,
        name: str,
        arguments: Mapping[str, Any],
        timeout_seconds: float,
        input_responses: Mapping[str, Any] | None = None,
        request_state: str | None = None,
    ) -> MCPToolResult:
        """调用远程工具，必要时携带多轮输入响应。"""

