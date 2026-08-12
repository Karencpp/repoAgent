"""MCP Gateway 确定性测试使用的记录型假 Client。"""

from __future__ import annotations

from collections import deque
from typing import Any, Mapping, Sequence

from .models import (
    MCPDiscoveryResult,
    MCPRequestContext,
    MCPToolListPage,
    MCPToolResult,
)


class RecordingMCPClient:
    """按脚本返回目录和工具结果，并记录每次请求上下文。"""

    def __init__(
        self,
        discovery: MCPDiscoveryResult,
        pages: Mapping[str | None, MCPToolListPage],
        results: Sequence[MCPToolResult | Exception],
    ) -> None:
        self.discovery_result = discovery
        self.pages = dict(pages)
        self.results = deque(results)
        self.discover_contexts: list[MCPRequestContext] = []
        self.list_calls: list[tuple[MCPRequestContext, str | None]] = []
        self.tool_calls: list[dict[str, Any]] = []

    def discover(self, context: MCPRequestContext) -> MCPDiscoveryResult:
        self.discover_contexts.append(context)
        return self.discovery_result

    def list_tools(
        self,
        context: MCPRequestContext,
        *,
        cursor: str | None = None,
    ) -> MCPToolListPage:
        self.list_calls.append((context, cursor))
        return self.pages[cursor]

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
        self.tool_calls.append(
            {
                "context": context,
                "name": name,
                "arguments": dict(arguments),
                "timeout_seconds": timeout_seconds,
                "input_responses": (
                    dict(input_responses)
                    if input_responses is not None
                    else None
                ),
                "request_state": request_state,
            }
        )
        if not self.results:
            raise RuntimeError("假 MCP Client 没有剩余工具结果")
        result = self.results.popleft()
        if isinstance(result, Exception):
            raise result
        return result

