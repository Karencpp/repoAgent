"""MCP 发现、工具映射、调用结果和恢复校验使用的领域模型。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


MCP_PROTOCOL_VERSION = "2026-07-28"


class MCPModel(BaseModel):
    """MCP 集成层统一使用严格且不可变的数据模型。"""

    model_config = ConfigDict(extra="forbid", frozen=True)


class MCPImplementation(MCPModel):
    """协议参与方的稳定软件身份。"""

    name: str = Field(min_length=1, max_length=200)
    version: str = Field(min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=1000)


class MCPRequestContext(MCPModel):
    """现代 MCP 每个请求都携带的版本、身份和客户端能力。"""

    protocol_version: str = MCP_PROTOCOL_VERSION
    client_info: MCPImplementation
    client_capabilities: dict[str, Any] = Field(default_factory=dict)


class MCPServerCapabilities(MCPModel):
    """Server 通过 discover 声明的可用能力。"""

    tools: bool = False
    resources: bool = False
    prompts: bool = False
    extensions: dict[str, Any] = Field(default_factory=dict)


class MCPDiscoveryResult(MCPModel):
    """server/discover 的领域化响应。"""

    supported_versions: tuple[str, ...] = Field(min_length=1)
    capabilities: MCPServerCapabilities
    server_info: MCPImplementation
    instructions: str | None = Field(default=None, max_length=10_000)


class MCPToolDescriptor(MCPModel):
    """MCP Server 声明的远程工具结构。"""

    name: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9_.-]+$",
    )
    description: str | None = Field(default=None, max_length=20_000)
    input_schema: dict[str, Any]
    output_schema: dict[str, Any] | None = None
    annotations: dict[str, Any] = Field(default_factory=dict)


class MCPToolListPage(MCPModel):
    """支持分页和现代缓存提示的工具列表页。"""

    tools: tuple[MCPToolDescriptor, ...]
    next_cursor: str | None = Field(default=None, max_length=1000)
    ttl_ms: int | None = Field(default=None, ge=0)
    cache_scope: str | None = Field(default=None, max_length=100)


class MCPCompleteToolResult(MCPModel):
    """已经完成的 MCP 工具结果。"""

    result_type: Literal["complete"] = "complete"
    content: tuple[dict[str, Any], ...] = ()
    structured_content: dict[str, Any] | None = None
    is_error: bool = False
    private_meta: dict[str, Any] = Field(default_factory=dict)


class MCPInputRequiredResult(MCPModel):
    """需要宿主向用户收集额外输入后重试的结果。"""

    result_type: Literal["input_required"] = "input_required"
    input_requests: dict[str, dict[str, Any]] = Field(min_length=1)
    request_state: str | None = Field(default=None, max_length=100_000)
    private_meta: dict[str, Any] = Field(default_factory=dict)


MCPToolResult = MCPCompleteToolResult | MCPInputRequiredResult


class MCPToolPolicy(MCPModel):
    """宿主审核后的单个远程工具映射和风险策略。"""

    remote_name: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9_.-]+$",
    )
    local_name: str | None = Field(
        default=None,
        min_length=1,
        max_length=100,
        pattern=r"^[A-Za-z][A-Za-z0-9_-]*$",
    )
    description: str = Field(min_length=1, max_length=1000)
    input_schema: dict[str, Any]
    output_schema: dict[str, Any] | None = None
    access: Literal["read", "execute"]
    executes_project_code: bool = False
    requires_explicit_authorization: bool = True


class MCPServerPolicy(MCPModel):
    """一个外部 Server 的稳定别名、版本范围和工具白名单。"""

    server_id: str = Field(
        min_length=1,
        max_length=40,
        pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$",
    )
    supported_protocol_versions: tuple[str, ...] = (MCP_PROTOCOL_VERSION,)
    tools: tuple[MCPToolPolicy, ...]
    request_timeout_seconds: float = Field(default=20.0, gt=0, le=300)

    @model_validator(mode="after")
    def validate_unique_tools(self) -> "MCPServerPolicy":
        """拒绝远程名称或显式本地名称重复。"""

        remote_names = [tool.remote_name for tool in self.tools]
        if len(remote_names) != len(set(remote_names)):
            raise ValueError("MCP 工具策略包含重复 remote_name")
        local_names = [
            tool.local_name for tool in self.tools if tool.local_name is not None
        ]
        if len(local_names) != len(set(local_names)):
            raise ValueError("MCP 工具策略包含重复 local_name")
        return self


class MCPMappedTool(MCPModel):
    """远程发现结果与宿主审核策略合并后的本地工具。"""

    server_id: str
    remote_name: str
    local_name: str
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any] | None = None
    access: Literal["read", "execute"]
    executes_project_code: bool
    requires_explicit_authorization: bool


class MCPServerSnapshot(MCPModel):
    """写入运行状态用于检测远程能力漂移的快照。"""

    server_id: str
    protocol_version: str
    server_name: str
    server_version: str
    capability_hash: str = Field(min_length=64, max_length=64)
    tool_catalog_hash: str = Field(min_length=64, max_length=64)
    mapped_tools: tuple[str, ...]


class MCPPendingInput(MCPModel):
    """宿主保留的多轮工具调用待输入状态。"""

    pending_id: str
    server_id: str
    remote_name: str
    arguments: dict[str, Any]
    input_requests: dict[str, dict[str, Any]]
    request_state: str | None = None


class MCPAuditEvent(MCPModel):
    """不进入模型上下文的 MCP 调用审计事件。"""

    server_id: str
    remote_name: str
    status: Literal[
        "completed",
        "tool_error",
        "input_required",
        "invalid_arguments",
        "protocol_error",
        "timeout",
    ]
    private_meta_hash: str | None = None
