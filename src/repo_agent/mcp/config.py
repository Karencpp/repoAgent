"""MCP Server 本地配置文件加载与应用装配。"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from repo_agent.tools.registry import ToolRegistry

from .gateway import MCPGateway, MCPGatewayConfig
from .http import ModernHTTPMCPClient
from .models import MCPImplementation, MCPServerPolicy, MCPServerSnapshot
from .server import RegistryMCPServer


class MCPConfigModel(BaseModel):
    """MCP 配置模型的公共严格配置。"""

    model_config = ConfigDict(extra="forbid", frozen=True)


class MCPHTTPServerConfig(MCPConfigModel):
    """一个 Streamable HTTP MCP Server 的连接配置。"""

    transport: Literal["http"] = "http"
    endpoint: str = Field(min_length=1, max_length=2_000)
    headers: dict[str, str] = Field(default_factory=dict)
    allow_insecure_localhost: bool = False
    policy: MCPServerPolicy


class MCPRegistryServerConfig(MCPConfigModel):
    """一个由当前 Tool Registry 导出的本地 MCP Server 配置。"""

    transport: Literal["registry"] = "registry"
    server_name: str = Field(default="repo-agent-local-registry", min_length=1)
    server_version: str = Field(default="1.0.0", min_length=1)
    exported_tools: tuple[str, ...] = Field(min_length=1)
    policy: MCPServerPolicy


MCPServerConfig = MCPHTTPServerConfig | MCPRegistryServerConfig


class MCPHostConfig(MCPConfigModel):
    """RepoAgent 启动时加载的 MCP 宿主配置。"""

    gateway: MCPGatewayConfig = Field(default_factory=MCPGatewayConfig)
    servers: tuple[MCPServerConfig, ...] = ()


def load_mcp_host_config(path: str | Path) -> MCPHostConfig:
    """读取并严格校验 MCP 配置 JSON 文件。"""

    config_path = Path(path).expanduser().resolve()
    if not config_path.is_file():
        raise FileNotFoundError(f"MCP 配置文件不存在：{config_path}")
    return MCPHostConfig.model_validate_json(config_path.read_text(encoding="utf-8"))


def attach_configured_mcp_servers(
    *,
    registry: ToolRegistry,
    config: MCPHostConfig,
) -> tuple[MCPGateway, tuple[MCPServerSnapshot, ...]]:
    """按配置发现、审核并注册 MCP 工具。"""

    gateway = MCPGateway(config=config.gateway)
    snapshots: list[MCPServerSnapshot] = []
    for server in config.servers:
        if server.transport == "registry":
            port = RegistryMCPServer(
                registry,
                server_info=MCPImplementation(
                    name=server.server_name,
                    version=server.server_version,
                ),
                exported_tools=server.exported_tools,
            )
        else:
            port = ModernHTTPMCPClient(
                server.endpoint,
                headers=server.headers,
                allow_insecure_localhost=server.allow_insecure_localhost,
            )
        gateway.attach(port, server.policy)
        snapshots.append(gateway.register_tools(registry, server.policy.server_id))
    return gateway, tuple(snapshots)
