"""RepoAgent 的现代 MCP 能力发现、策略映射和调用网关。"""

from .config import (
    MCPHostConfig,
    MCPHTTPServerConfig,
    MCPRegistryServerConfig,
    attach_configured_mcp_servers,
    load_mcp_host_config,
)
from .fakes import RecordingMCPClient
from .gateway import (
    MCPCapabilityDriftError,
    MCPGateway,
    MCPGatewayConfig,
    MCPGatewayError,
    MCPProtocolCompatibilityError,
    MCPResultValidationError,
    MCPToolCatalogError,
)
from .http import MCPHTTPError, MCPRemoteProtocolError, ModernHTTPMCPClient
from .models import (
    MCP_PROTOCOL_VERSION,
    MCPAuditEvent,
    MCPCompleteToolResult,
    MCPDiscoveryResult,
    MCPImplementation,
    MCPInputRequiredResult,
    MCPMappedTool,
    MCPPendingInput,
    MCPRequestContext,
    MCPServerCapabilities,
    MCPServerPolicy,
    MCPServerSnapshot,
    MCPToolDescriptor,
    MCPToolListPage,
    MCPToolPolicy,
    MCPToolResult,
)
from .port import MCPClientPort
from .server import RegistryMCPServer

__all__ = [
    "MCP_PROTOCOL_VERSION",
    "MCPAuditEvent",
    "MCPClientPort",
    "MCPCompleteToolResult",
    "MCPCapabilityDriftError",
    "MCPDiscoveryResult",
    "MCPGateway",
    "MCPGatewayConfig",
    "MCPGatewayError",
    "MCPHostConfig",
    "MCPHTTPServerConfig",
    "MCPImplementation",
    "MCPHTTPError",
    "MCPInputRequiredResult",
    "MCPMappedTool",
    "MCPPendingInput",
    "MCPProtocolCompatibilityError",
    "MCPRequestContext",
    "MCPRegistryServerConfig",
    "MCPRemoteProtocolError",
    "MCPResultValidationError",
    "MCPServerCapabilities",
    "MCPServerPolicy",
    "MCPServerSnapshot",
    "MCPToolCatalogError",
    "MCPToolDescriptor",
    "MCPToolListPage",
    "MCPToolPolicy",
    "MCPToolResult",
    "ModernHTTPMCPClient",
    "RecordingMCPClient",
    "RegistryMCPServer",
    "attach_configured_mcp_servers",
    "load_mcp_host_config",
]
