from __future__ import annotations

import json
from pathlib import Path
import sys
import unittest

import httpx


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from repo_agent.mcp import (
    MCP_PROTOCOL_VERSION,
    MCPGateway,
    MCPHTTPError,
    MCPImplementation,
    MCPInputRequiredResult,
    MCPRemoteProtocolError,
    MCPRequestContext,
    MCPServerPolicy,
    MCPToolPolicy,
    ModernHTTPMCPClient,
)
from repo_agent.tools import ToolRegistry


INPUT_SCHEMA = {
    "type": "object",
    "properties": {"query": {"type": "string"}},
    "required": ["query"],
    "additionalProperties": False,
}


def request_context() -> MCPRequestContext:
    """构造现代协议请求元数据。"""

    return MCPRequestContext(
        client_info=MCPImplementation(
            name="http-test-client",
            version="1.0.0",
        )
    )


def jsonrpc_result(request: httpx.Request, result: dict[str, object]) -> httpx.Response:
    """使用请求中的 id 构造 JSON-RPC 成功响应。"""

    body = json.loads(request.content)
    return httpx.Response(
        200,
        headers={"Content-Type": "application/json"},
        json={
            "jsonrpc": "2.0",
            "id": body["id"],
            "result": result,
        },
    )


class ModernHTTPMCPClientTests(unittest.TestCase):
    def test_http_adapter_and_gateway_work_end_to_end(self) -> None:
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            body = json.loads(request.content)
            method = body["method"]
            if method == "server/discover":
                return jsonrpc_result(
                    request,
                    {
                        "resultType": "complete",
                        "supportedVersions": [MCP_PROTOCOL_VERSION],
                        "capabilities": {"tools": {}},
                        "serverInfo": {
                            "name": "http-search",
                            "version": "1.0.0",
                        },
                    },
                )
            if method == "tools/list":
                return jsonrpc_result(
                    request,
                    {
                        "resultType": "complete",
                        "tools": [
                            {
                                "name": "search",
                                "description": "不可信远程说明",
                                "inputSchema": INPUT_SCHEMA,
                            }
                        ],
                        "ttlMs": 60_000,
                        "cacheScope": "private",
                    },
                )
            return jsonrpc_result(
                request,
                {
                    "resultType": "complete",
                    "content": [{"type": "text", "text": "ISSUE-7"}],
                    "structuredContent": {"items": ["ISSUE-7"]},
                    "_meta": {"trace": "private"},
                },
            )

        http_client = httpx.Client(transport=httpx.MockTransport(handler))
        port = ModernHTTPMCPClient(
            "https://mcp.example.test/mcp",
            client=http_client,
        )
        gateway = MCPGateway()
        gateway.attach(
            port,
            MCPServerPolicy(
                server_id="http-search",
                tools=(
                    MCPToolPolicy(
                        remote_name="search",
                        local_name="mcp_http_search",
                        description="搜索已授权的远程工单。",
                        input_schema=INPUT_SCHEMA,
                        access="read",
                    ),
                ),
            ),
        )
        registry = ToolRegistry()
        gateway.register_tools(registry, "http-search")

        result = registry.dispatch("mcp_http_search", {"query": "bug"})

        self.assertTrue(result.ok)
        self.assertEqual(len(requests), 3)
        for request in requests:
            body = json.loads(request.content)
            self.assertEqual(
                request.headers["mcp-protocol-version"],
                MCP_PROTOCOL_VERSION,
            )
            self.assertEqual(request.headers["mcp-method"], body["method"])
            self.assertNotIn("mcp-session-id", request.headers)
            meta = body["params"]["_meta"]
            self.assertEqual(
                meta["io.modelcontextprotocol/protocolVersion"],
                MCP_PROTOCOL_VERSION,
            )
            self.assertIn("io.modelcontextprotocol/clientInfo", meta)
        self.assertEqual(requests[-1].headers["mcp-name"], "search")

    def test_http_adapter_parses_input_required_result(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return jsonrpc_result(
                request,
                {
                    "resultType": "input_required",
                    "inputRequests": {
                        "confirm": {
                            "type": "elicitation",
                            "message": "是否继续？",
                            "schema": {"type": "boolean"},
                        }
                    },
                    "requestState": "opaque",
                },
            )

        client = ModernHTTPMCPClient(
            "https://mcp.example.test/mcp",
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        )

        result = client.call_tool(
            request_context(),
            name="delete",
            arguments={"id": "7"},
            timeout_seconds=1,
        )

        self.assertIsInstance(result, MCPInputRequiredResult)
        self.assertEqual(result.request_state, "opaque")
        self.assertIn("confirm", result.input_requests)

    def test_json_rpc_error_is_not_treated_as_tool_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            return httpx.Response(
                200,
                headers={"Content-Type": "application/json"},
                json={
                    "jsonrpc": "2.0",
                    "id": body["id"],
                    "error": {
                        "code": -32022,
                        "message": "Unsupported protocol version",
                    },
                },
            )

        client = ModernHTTPMCPClient(
            "https://mcp.example.test/mcp",
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        )

        with self.assertRaises(MCPRemoteProtocolError):
            client.discover(request_context())

    def test_mismatched_json_rpc_id_is_rejected(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"Content-Type": "application/json"},
                json={
                    "jsonrpc": "2.0",
                    "id": 999,
                    "result": {},
                },
            )

        client = ModernHTTPMCPClient(
            "https://mcp.example.test/mcp",
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        )

        with self.assertRaises(MCPRemoteProtocolError):
            client.discover(request_context())

    def test_remote_endpoint_requires_https_but_localhost_can_be_explicit(self) -> None:
        with self.assertRaises(ValueError):
            ModernHTTPMCPClient("http://mcp.example.test/mcp")

        client = ModernHTTPMCPClient(
            "http://127.0.0.1:8080/mcp",
            allow_insecure_localhost=True,
        )

        self.assertTrue(client.endpoint.startswith("http://127.0.0.1"))

    def test_non_json_response_is_rejected(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"Content-Type": "text/html"},
                text="<html>not mcp</html>",
            )

        client = ModernHTTPMCPClient(
            "https://mcp.example.test/mcp",
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        )

        with self.assertRaises(MCPHTTPError):
            client.discover(request_context())


if __name__ == "__main__":
    unittest.main()
