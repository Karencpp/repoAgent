"""把现有 Tool Registry 暴露为协议无关的内存 MCP Server。"""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from enum import Enum
import json
from pathlib import Path
from typing import Any, Iterable, Mapping

from pydantic import BaseModel

from repo_agent.tools.registry import ToolRegistry

from .models import (
    MCPCompleteToolResult,
    MCPDiscoveryResult,
    MCPImplementation,
    MCPRequestContext,
    MCPServerCapabilities,
    MCPToolDescriptor,
    MCPToolListPage,
    MCPToolResult,
)


def _to_jsonable(value: Any) -> Any:
    """把本地工具结果转换成协议可序列化数据。"""

    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if is_dataclass(value) and not isinstance(value, type):
        return {key: _to_jsonable(item) for key, item in asdict(value).items()}
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _to_jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list, set)):
        return [_to_jsonable(item) for item in value]
    return value


class RegistryMCPServer:
    """用于端到端测试和嵌入式部署的 Tool Registry Server 适配器。"""

    def __init__(
        self,
        registry: ToolRegistry,
        *,
        server_info: MCPImplementation,
        exported_tools: Iterable[str],
        supported_versions: tuple[str, ...] = ("2026-07-28",),
        page_size: int = 50,
        ttl_ms: int = 30_000,
    ) -> None:
        if not 1 <= page_size <= 200:
            raise ValueError("page_size 必须在 1 到 200 之间")
        self.registry = registry
        self.server_info = server_info
        self.exported_tools = tuple(sorted(set(exported_tools)))
        self.supported_versions = supported_versions
        self.page_size = page_size
        self.ttl_ms = ttl_ms
        registered = {
            tool.name for tool in registry.model_tools(self.exported_tools)
        }
        missing = sorted(set(self.exported_tools) - registered)
        if missing:
            raise ValueError(f"导出的本地工具不存在：{missing}")

    def _validate_context(self, context: MCPRequestContext) -> None:
        """现代协议每次请求都验证版本和客户端身份。"""

        if context.protocol_version not in self.supported_versions:
            raise ValueError(
                f"不支持 MCP 协议版本：{context.protocol_version}"
            )
        if not context.client_info.name.strip():
            raise ValueError("MCP 请求缺少 clientInfo")

    def discover(self, context: MCPRequestContext) -> MCPDiscoveryResult:
        """返回 Server 身份、能力和支持版本。"""

        if not context.client_info.name.strip():
            raise ValueError("MCP discover 缺少 clientInfo")
        return MCPDiscoveryResult(
            supported_versions=self.supported_versions,
            capabilities=MCPServerCapabilities(tools=True),
            server_info=self.server_info,
            instructions="工具调用仍需由宿主执行权限和用户授权控制。",
        )

    def list_tools(
        self,
        context: MCPRequestContext,
        *,
        cursor: str | None = None,
    ) -> MCPToolListPage:
        """以稳定顺序分页返回显式导出的本地工具。"""

        self._validate_context(context)
        try:
            start = int(cursor) if cursor is not None else 0
        except ValueError as exc:
            raise ValueError("MCP tools/list cursor 非法") from exc
        definitions = self.registry.model_tools(self.exported_tools)
        selected = definitions[start : start + self.page_size]
        next_offset = start + len(selected)
        next_cursor = (
            str(next_offset) if next_offset < len(definitions) else None
        )
        return MCPToolListPage(
            tools=tuple(
                MCPToolDescriptor(
                    name=tool.name,
                    description=tool.description,
                    input_schema=tool.input_schema,
                )
                for tool in selected
            ),
            next_cursor=next_cursor,
            ttl_ms=self.ttl_ms,
            cache_scope="private",
        )

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
        """调用导出白名单中的本地工具并规范化协议结果。"""

        self._validate_context(context)
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds 必须大于 0")
        if input_responses is not None or request_state is not None:
            raise ValueError("当前 Registry Server 不需要多轮输入")
        result = self.registry.dispatch(
            name,
            arguments,
            allowed_tools=self.exported_tools,
        )
        structured = {
            "status": result.status,
            "data": _to_jsonable(result.data),
            "error": _to_jsonable(result.error),
        }
        text = json.dumps(structured, ensure_ascii=False, sort_keys=True)
        return MCPCompleteToolResult(
            content=({"type": "text", "text": text},),
            structured_content=structured,
            is_error=not result.ok,
            private_meta={"server_adapter": "registry"},
        )

