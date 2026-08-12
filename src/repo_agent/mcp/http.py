"""面向 MCP 2026-07-28 的无会话 JSON Streamable HTTP 适配器。"""

from __future__ import annotations

from itertools import count
import json
from threading import Lock
from typing import Any, Mapping

import httpx

from .models import (
    MCPCompleteToolResult,
    MCPDiscoveryResult,
    MCPImplementation,
    MCPInputRequiredResult,
    MCPRequestContext,
    MCPServerCapabilities,
    MCPToolDescriptor,
    MCPToolListPage,
    MCPToolResult,
)


class MCPHTTPError(RuntimeError):
    """Streamable HTTP 状态、媒体类型或响应大小不合法。"""


class MCPRemoteProtocolError(RuntimeError):
    """远程返回 JSON-RPC error 或不完整结果。"""


_RESERVED_HEADERS = {
    "content-type",
    "accept",
    "mcp-protocol-version",
    "mcp-method",
    "mcp-name",
}


class ModernHTTPMCPClient:
    """实现现代无会话 MCP 的同步 JSON 请求子集。"""

    def __init__(
        self,
        endpoint: str,
        *,
        client: httpx.Client | None = None,
        headers: Mapping[str, str] | None = None,
        allow_insecure_localhost: bool = False,
        max_response_bytes: int = 1_000_000,
    ) -> None:
        url = httpx.URL(endpoint)
        if url.scheme not in {"http", "https"} or not url.host:
            raise ValueError("MCP HTTP endpoint 必须是完整 http(s) URL")
        is_localhost = url.host in {"localhost", "127.0.0.1", "::1"}
        if url.scheme != "https" and not (
            allow_insecure_localhost and is_localhost
        ):
            raise ValueError("远程 MCP HTTP endpoint 必须使用 HTTPS")
        if url.username or url.password:
            raise ValueError("MCP endpoint 不允许在 URL 中嵌入凭据")
        custom_headers = dict(headers or {})
        forbidden = sorted(
            key
            for key in custom_headers
            if key.casefold() in _RESERVED_HEADERS
        )
        if forbidden:
            raise ValueError(f"不能覆盖 MCP 保留请求头：{forbidden}")
        if max_response_bytes < 1_024:
            raise ValueError("max_response_bytes 不能小于 1024")
        self.endpoint = str(url)
        self.client = client or httpx.Client(follow_redirects=False)
        self.headers = custom_headers
        self.max_response_bytes = max_response_bytes
        self._ids = count(1)
        self._id_lock = Lock()

    def _next_id(self) -> int:
        """生成进程内唯一的 JSON-RPC 请求 ID。"""

        with self._id_lock:
            return next(self._ids)

    @staticmethod
    def _request_meta(context: MCPRequestContext) -> dict[str, Any]:
        """构造现代协议每个请求都必须携带的保留元数据。"""

        return {
            "io.modelcontextprotocol/protocolVersion": (
                context.protocol_version
            ),
            "io.modelcontextprotocol/clientInfo": (
                context.client_info.model_dump(
                    mode="json",
                    exclude_none=True,
                )
            ),
            "io.modelcontextprotocol/clientCapabilities": (
                context.client_capabilities
            ),
        }

    def _request(
        self,
        context: MCPRequestContext,
        method: str,
        params: Mapping[str, Any],
        *,
        name: str | None = None,
        timeout_seconds: float = 20.0,
    ) -> Mapping[str, Any]:
        """发送一条自包含 JSON-RPC 请求并返回 result 对象。"""

        request_params = dict(params)
        request_params["_meta"] = self._request_meta(context)
        request_id = self._next_id()
        body = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": request_params,
        }
        request_headers = {
            **self.headers,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "MCP-Protocol-Version": context.protocol_version,
            "Mcp-Method": method,
        }
        if name is not None:
            request_headers["Mcp-Name"] = name
        response = self.client.post(
            self.endpoint,
            headers=request_headers,
            json=body,
            timeout=timeout_seconds,
        )
        if response.history:
            raise MCPHTTPError("MCP HTTP 请求发生了不允许的重定向")
        if len(response.content) > self.max_response_bytes:
            raise MCPHTTPError("MCP HTTP 响应超过大小上限")
        if response.status_code >= 400:
            raise MCPHTTPError(
                f"MCP HTTP 状态异常：{response.status_code}"
            )
        content_type = response.headers.get("content-type", "").casefold()
        if "application/json" not in content_type:
            raise MCPHTTPError(
                f"当前适配器只接受 JSON 响应：{content_type or '缺失'}"
            )
        try:
            payload = response.json()
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise MCPHTTPError("MCP HTTP 响应不是合法 JSON") from exc
        if not isinstance(payload, Mapping) or payload.get("jsonrpc") != "2.0":
            raise MCPRemoteProtocolError("MCP 响应不是 JSON-RPC 2.0 对象")
        if payload.get("id") != request_id:
            raise MCPRemoteProtocolError("MCP JSON-RPC 响应 id 与请求不一致")
        if "error" in payload:
            error = payload["error"]
            if not isinstance(error, Mapping):
                raise MCPRemoteProtocolError("MCP JSON-RPC error 结构非法")
            raise MCPRemoteProtocolError(
                f"MCP JSON-RPC 错误 {error.get('code')}："
                f"{error.get('message')}"
            )
        result = payload.get("result")
        if not isinstance(result, Mapping):
            raise MCPRemoteProtocolError("MCP JSON-RPC 响应缺少 result")
        return result

    def discover(self, context: MCPRequestContext) -> MCPDiscoveryResult:
        """调用 server/discover 并规范化 Server 能力。"""

        raw = self._request(context, "server/discover", {})
        capabilities = raw.get("capabilities") or {}
        if not isinstance(capabilities, Mapping):
            raise MCPRemoteProtocolError("MCP capabilities 必须是对象")
        server_info = raw.get("serverInfo")
        if not isinstance(server_info, Mapping):
            raise MCPRemoteProtocolError("MCP discover 缺少 serverInfo")
        supported = raw.get("supportedVersions")
        if not isinstance(supported, list):
            raise MCPRemoteProtocolError(
                "MCP discover 缺少 supportedVersions"
            )
        return MCPDiscoveryResult(
            supported_versions=tuple(str(item) for item in supported),
            capabilities=MCPServerCapabilities(
                tools=capabilities.get("tools") is not None,
                resources=capabilities.get("resources") is not None,
                prompts=capabilities.get("prompts") is not None,
                extensions=dict(capabilities.get("extensions") or {}),
            ),
            server_info=MCPImplementation(
                name=str(server_info.get("name", "")),
                version=str(server_info.get("version", "")),
                description=server_info.get("description"),
            ),
            instructions=raw.get("instructions"),
        )

    def list_tools(
        self,
        context: MCPRequestContext,
        *,
        cursor: str | None = None,
    ) -> MCPToolListPage:
        """调用 tools/list 并保留分页和缓存提示。"""

        params = {"cursor": cursor} if cursor is not None else {}
        raw = self._request(context, "tools/list", params)
        raw_tools = raw.get("tools")
        if not isinstance(raw_tools, list):
            raise MCPRemoteProtocolError("MCP tools/list 缺少 tools 数组")
        tools: list[MCPToolDescriptor] = []
        for item in raw_tools:
            if not isinstance(item, Mapping):
                raise MCPRemoteProtocolError("MCP Tool 必须是对象")
            input_schema = item.get("inputSchema")
            if not isinstance(input_schema, Mapping):
                raise MCPRemoteProtocolError(
                    f"MCP Tool 缺少 inputSchema：{item.get('name')}"
                )
            output_schema = item.get("outputSchema")
            tools.append(
                MCPToolDescriptor(
                    name=str(item.get("name", "")),
                    description=item.get("description"),
                    input_schema=dict(input_schema),
                    output_schema=(
                        dict(output_schema)
                        if isinstance(output_schema, Mapping)
                        else None
                    ),
                    annotations=dict(item.get("annotations") or {}),
                )
            )
        return MCPToolListPage(
            tools=tuple(tools),
            next_cursor=raw.get("nextCursor"),
            ttl_ms=raw.get("ttlMs"),
            cache_scope=raw.get("cacheScope"),
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
        """调用 tools/call，并保留 complete 与 input_required 分支。"""

        params: dict[str, Any] = {
            "name": name,
            "arguments": dict(arguments),
        }
        if input_responses is not None:
            params["inputResponses"] = dict(input_responses)
        if request_state is not None:
            params["requestState"] = request_state
        raw = self._request(
            context,
            "tools/call",
            params,
            name=name,
            timeout_seconds=timeout_seconds,
        )
        result_type = raw.get("resultType", "complete")
        private_meta = raw.get("_meta")
        if result_type == "input_required":
            input_requests = raw.get("inputRequests")
            if not isinstance(input_requests, Mapping):
                raise MCPRemoteProtocolError(
                    "MCP input_required 缺少 inputRequests"
                )
            if not all(
                isinstance(value, Mapping)
                for value in input_requests.values()
            ):
                raise MCPRemoteProtocolError(
                    "MCP inputRequests 中的每一项都必须是对象"
                )
            return MCPInputRequiredResult(
                input_requests={
                    str(key): dict(value)
                    for key, value in input_requests.items()
                    if isinstance(value, Mapping)
                },
                request_state=raw.get("requestState"),
                private_meta=(
                    dict(private_meta)
                    if isinstance(private_meta, Mapping)
                    else {}
                ),
            )
        if result_type != "complete":
            raise MCPRemoteProtocolError(
                f"当前适配器不支持 MCP resultType：{result_type}"
            )
        raw_content = raw.get("content") or []
        if not isinstance(raw_content, list) or not all(
            isinstance(item, Mapping) for item in raw_content
        ):
            raise MCPRemoteProtocolError("MCP complete content 必须是对象数组")
        structured = raw.get("structuredContent")
        return MCPCompleteToolResult(
            content=tuple(dict(item) for item in raw_content),
            structured_content=(
                dict(structured) if isinstance(structured, Mapping) else None
            ),
            is_error=bool(raw.get("isError", False)),
            private_meta=(
                dict(private_meta)
                if isinstance(private_meta, Mapping)
                else {}
            ),
        )
