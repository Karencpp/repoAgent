"""把外部 MCP 工具安全映射到 RepoAgent Tool Registry。"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import time
from typing import Any, Callable, Mapping
from uuid import uuid4

from jsonschema import SchemaError as JSONSchemaError
from jsonschema import ValidationError as JSONValidationError
from jsonschema.validators import validator_for

from repo_agent.tools.catalog import ToolDefinition
from repo_agent.tools.models import ToolErrorKind, ToolResult
from repo_agent.tools.registry import ToolRegistry

from .models import (
    MCPAuditEvent,
    MCPCompleteToolResult,
    MCPDiscoveryResult,
    MCPImplementation,
    MCPInputRequiredResult,
    MCPMappedTool,
    MCPPendingInput,
    MCPRequestContext,
    MCPServerPolicy,
    MCPServerSnapshot,
    MCPToolDescriptor,
    MCPToolListPage,
)
from .port import MCPClientPort


class MCPGatewayError(RuntimeError):
    """MCP 发现、策略映射或调用失败的基类。"""


class MCPProtocolCompatibilityError(MCPGatewayError):
    """Client 与 Server 没有共同支持的协议版本。"""


class MCPToolCatalogError(MCPGatewayError):
    """远程工具目录不满足分页、唯一性或宿主策略。"""


class MCPCapabilityDriftError(MCPGatewayError):
    """已绑定或已恢复运行的远程能力发生变化。"""


class MCPResultValidationError(MCPGatewayError):
    """远程结果超过边界或不满足宿主审核的输出 Schema。"""


@dataclass(frozen=True, slots=True)
class MCPGatewayConfig:
    """限制远程目录、Schema 和单次结果规模。"""

    max_pages: int = 20
    max_tools_per_server: int = 200
    max_schema_bytes: int = 64_000
    max_schema_depth: int = 20
    max_result_bytes: int = 100_000
    max_content_blocks: int = 100

    def __post_init__(self) -> None:
        if not 1 <= self.max_pages <= 100:
            raise ValueError("max_pages 必须在 1 到 100 之间")
        if not 1 <= self.max_tools_per_server <= 2_000:
            raise ValueError("max_tools_per_server 必须在 1 到 2000 之间")
        if self.max_schema_bytes < 1_024:
            raise ValueError("max_schema_bytes 不能小于 1024")
        if not 2 <= self.max_schema_depth <= 100:
            raise ValueError("max_schema_depth 必须在 2 到 100 之间")
        if self.max_result_bytes < 1_024:
            raise ValueError("max_result_bytes 不能小于 1024")
        if not 1 <= self.max_content_blocks <= 1_000:
            raise ValueError("max_content_blocks 必须在 1 到 1000 之间")


@dataclass(slots=True)
class _AttachedServer:
    """Gateway 内部保存的远程连接、策略和目录缓存。"""

    port: MCPClientPort
    policy: MCPServerPolicy
    protocol_version: str | None = None
    discovery: MCPDiscoveryResult | None = None
    mapped_tools: tuple[MCPMappedTool, ...] = ()
    snapshot: MCPServerSnapshot | None = None
    cache_deadline: float = 0.0
    bound: bool = False


def _stable_hash(value: Any) -> str:
    """为协议快照生成稳定 SHA-256。"""

    serialized = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _json_size(value: Any) -> int:
    """计算规范 JSON 的 UTF-8 字节数。"""

    serialized = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return len(serialized.encode("utf-8"))


def _check_schema_tree(
    value: Any,
    *,
    depth: int,
    max_depth: int,
) -> None:
    """拒绝引用解析和可能造成本地正则拒绝服务的 Schema。"""

    if depth > max_depth:
        raise MCPToolCatalogError("MCP JSON Schema 嵌套超过上限")
    if isinstance(value, Mapping):
        for key, child in value.items():
            if key in {"$ref", "$dynamicRef", "$recursiveRef"}:
                raise MCPToolCatalogError("当前 MCP 网关不接受 JSON Schema 引用")
            if key == "pattern":
                raise MCPToolCatalogError("当前 MCP 网关不接受远程正则约束")
            _check_schema_tree(child, depth=depth + 1, max_depth=max_depth)
    elif isinstance(value, list):
        for child in value:
            _check_schema_tree(child, depth=depth + 1, max_depth=max_depth)


def _validate_schema(
    schema: Mapping[str, Any],
    *,
    config: MCPGatewayConfig,
    require_object: bool,
) -> dict[str, Any]:
    """校验宿主或远程 Schema 的大小、深度和 JSON Schema 合法性。"""

    candidate = json.loads(json.dumps(dict(schema), ensure_ascii=False))
    if _json_size(candidate) > config.max_schema_bytes:
        raise MCPToolCatalogError("MCP JSON Schema 超过大小上限")
    _check_schema_tree(
        candidate,
        depth=0,
        max_depth=config.max_schema_depth,
    )
    if require_object and candidate.get("type") != "object":
        raise MCPToolCatalogError("MCP 工具 inputSchema 根类型必须是 object")
    try:
        validator_type = validator_for(candidate)
        validator_type.check_schema(candidate)
    except JSONSchemaError as exc:
        raise MCPToolCatalogError(f"MCP JSON Schema 非法：{exc.message}") from exc
    return candidate


_ANNOTATION_KEYS = {
    "description",
    "title",
    "$comment",
    "examples",
    "default",
    "deprecated",
    "readOnly",
    "writeOnly",
}


def _schema_structure(value: Any) -> Any:
    """移除仅供展示的注解，只比较会影响参数结构和校验的部分。"""

    if isinstance(value, Mapping):
        return {
            str(key): _schema_structure(child)
            for key, child in value.items()
            if key not in _ANNOTATION_KEYS
        }
    if isinstance(value, list):
        return [_schema_structure(child) for child in value]
    return value


def _local_tool_name(server_id: str, remote_name: str) -> str:
    """生成不依赖远程 serverInfo.name 的稳定本地命名空间。"""

    normalized_server = server_id.replace("-", "_")
    normalized_remote = remote_name.replace("-", "_").replace(".", "_")
    name = f"mcp__{normalized_server}__{normalized_remote}"
    if len(name) > 100:
        raise MCPToolCatalogError(
            f"MCP 映射工具名超过本地 100 字符限制：{remote_name}"
        )
    return name


def _strip_private_meta(value: Any) -> Any:
    """递归移除远程 `_meta`，避免宿主私有数据进入模型观察。"""

    if isinstance(value, Mapping):
        return {
            str(key): _strip_private_meta(child)
            for key, child in value.items()
            if key != "_meta"
        }
    if isinstance(value, (list, tuple)):
        return [_strip_private_meta(child) for child in value]
    return value


class MCPGateway:
    """聚合多个 MCP Server，并按宿主策略注册有限工具。"""

    def __init__(
        self,
        *,
        client_info: MCPImplementation | None = None,
        client_capabilities: Mapping[str, Any] | None = None,
        config: MCPGatewayConfig | None = None,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        self.client_info = client_info or MCPImplementation(
            name="repo-agent",
            version="0.1.0",
            description="面向代码仓库维护任务的可解释 Agent",
        )
        self.client_capabilities = dict(client_capabilities or {})
        self.config = config or MCPGatewayConfig()
        self.monotonic = monotonic or time.monotonic
        self._servers: dict[str, _AttachedServer] = {}
        self._pending: dict[str, MCPPendingInput] = {}
        self._audit: list[MCPAuditEvent] = []

    @property
    def audit_events(self) -> tuple[MCPAuditEvent, ...]:
        """返回不会自动进入模型上下文的 MCP 审计事件。"""

        return tuple(self._audit)

    @property
    def pending_inputs(self) -> tuple[MCPPendingInput, ...]:
        """返回等待宿主收集用户输入的工具调用。"""

        return tuple(self._pending[key] for key in sorted(self._pending))

    def attach(
        self,
        port: MCPClientPort,
        policy: MCPServerPolicy,
    ) -> None:
        """挂载一个外部 Server，并预校验宿主审核 Schema。"""

        if policy.server_id in self._servers:
            raise ValueError(f"MCP Server 已挂载：{policy.server_id}")
        for tool in policy.tools:
            _validate_schema(
                tool.input_schema,
                config=self.config,
                require_object=True,
            )
            if tool.output_schema is not None:
                _validate_schema(
                    tool.output_schema,
                    config=self.config,
                    require_object=False,
                )
        self._servers[policy.server_id] = _AttachedServer(
            port=port,
            policy=policy,
        )

    def _get_server(self, server_id: str) -> _AttachedServer:
        try:
            return self._servers[server_id]
        except KeyError as exc:
            raise MCPGatewayError(f"未挂载 MCP Server：{server_id}") from exc

    def _context(self, protocol_version: str) -> MCPRequestContext:
        """为每个现代 MCP 请求重新构造身份和能力元数据。"""

        return MCPRequestContext(
            protocol_version=protocol_version,
            client_info=self.client_info,
            client_capabilities=self.client_capabilities,
        )

    def _select_protocol(
        self,
        discovery: MCPDiscoveryResult,
        policy: MCPServerPolicy,
    ) -> str:
        """按宿主偏好选择第一个共同支持的协议版本。"""

        remote = set(discovery.supported_versions)
        for version in policy.supported_protocol_versions:
            if version in remote:
                return version
        raise MCPProtocolCompatibilityError(
            f"MCP Server {policy.server_id} 没有共同协议版本；"
            f"远程={discovery.supported_versions}，"
            f"本地={policy.supported_protocol_versions}"
        )

    def _list_all_tools(
        self,
        server: _AttachedServer,
        protocol_version: str,
    ) -> tuple[tuple[MCPToolDescriptor, ...], int | None]:
        """遍历工具分页并检测重复名称与循环 cursor。"""

        tools: list[MCPToolDescriptor] = []
        seen_names: set[str] = set()
        seen_cursors: set[str] = set()
        cursor: str | None = None
        ttl_values: list[int] = []
        for _page_number in range(1, self.config.max_pages + 1):
            page: MCPToolListPage = server.port.list_tools(
                self._context(protocol_version),
                cursor=cursor,
            )
            if page.ttl_ms is not None:
                ttl_values.append(page.ttl_ms)
            for tool in page.tools:
                if tool.name in seen_names:
                    raise MCPToolCatalogError(
                        f"MCP Server 返回重复工具名：{tool.name}"
                    )
                seen_names.add(tool.name)
                tools.append(tool)
                if len(tools) > self.config.max_tools_per_server:
                    raise MCPToolCatalogError("MCP Server 工具数量超过上限")
            if page.next_cursor is None:
                return tuple(tools), min(ttl_values) if ttl_values else None
            if page.next_cursor in seen_cursors:
                raise MCPToolCatalogError("MCP tools/list 出现循环 cursor")
            seen_cursors.add(page.next_cursor)
            cursor = page.next_cursor
        raise MCPToolCatalogError("MCP tools/list 分页超过上限")

    def _map_tools(
        self,
        policy: MCPServerPolicy,
        remote_tools: tuple[MCPToolDescriptor, ...],
    ) -> tuple[MCPMappedTool, ...]:
        """只映射宿主白名单，并核对审核 Schema 是否发生结构变化。"""

        remote_by_name = {tool.name: tool for tool in remote_tools}
        mapped: list[MCPMappedTool] = []
        local_names: set[str] = set()
        for tool_policy in policy.tools:
            remote = remote_by_name.get(tool_policy.remote_name)
            if remote is None:
                raise MCPToolCatalogError(
                    f"宿主策略中的远程工具不存在：{tool_policy.remote_name}"
                )
            remote_input = _validate_schema(
                remote.input_schema,
                config=self.config,
                require_object=True,
            )
            approved_input = _validate_schema(
                tool_policy.input_schema,
                config=self.config,
                require_object=True,
            )
            if _stable_hash(_schema_structure(remote_input)) != _stable_hash(
                _schema_structure(approved_input)
            ):
                raise MCPCapabilityDriftError(
                    f"MCP 工具 inputSchema 与宿主审核版本不一致："
                    f"{tool_policy.remote_name}"
                )

            approved_output: dict[str, Any] | None = None
            if tool_policy.output_schema is not None:
                if remote.output_schema is None:
                    raise MCPCapabilityDriftError(
                        f"MCP 工具缺少已审核 outputSchema："
                        f"{tool_policy.remote_name}"
                    )
                remote_output = _validate_schema(
                    remote.output_schema,
                    config=self.config,
                    require_object=False,
                )
                approved_output = _validate_schema(
                    tool_policy.output_schema,
                    config=self.config,
                    require_object=False,
                )
                if _stable_hash(_schema_structure(remote_output)) != _stable_hash(
                    _schema_structure(approved_output)
                ):
                    raise MCPCapabilityDriftError(
                        f"MCP 工具 outputSchema 与宿主审核版本不一致："
                        f"{tool_policy.remote_name}"
                    )

            local_name = tool_policy.local_name or _local_tool_name(
                policy.server_id,
                tool_policy.remote_name,
            )
            if local_name in local_names:
                raise MCPToolCatalogError(
                    f"多个远程工具映射到同一本地名称：{local_name}"
                )
            local_names.add(local_name)
            mapped.append(
                MCPMappedTool(
                    server_id=policy.server_id,
                    remote_name=tool_policy.remote_name,
                    local_name=local_name,
                    description=tool_policy.description,
                    input_schema=approved_input,
                    output_schema=approved_output,
                    access=tool_policy.access,
                    executes_project_code=tool_policy.executes_project_code,
                    requires_explicit_authorization=(
                        tool_policy.requires_explicit_authorization
                    ),
                )
            )
        mapped.sort(key=lambda item: item.local_name)
        return tuple(mapped)

    def _load_remote(
        self,
        server: _AttachedServer,
    ) -> tuple[
        str,
        MCPDiscoveryResult,
        tuple[MCPMappedTool, ...],
        MCPServerSnapshot,
        int | None,
    ]:
        """执行 discover、分页 list 和宿主策略映射。"""

        preferred_version = server.policy.supported_protocol_versions[0]
        discovery = server.port.discover(self._context(preferred_version))
        protocol_version = self._select_protocol(discovery, server.policy)
        if server.policy.tools and not discovery.capabilities.tools:
            raise MCPToolCatalogError("MCP Server 未声明 tools 能力")
        remote_tools, ttl_ms = self._list_all_tools(server, protocol_version)
        mapped = self._map_tools(server.policy, remote_tools)
        capability_hash = _stable_hash(
            discovery.capabilities.model_dump(mode="json")
        )
        tool_catalog_hash = _stable_hash(
            [
                {
                    "name": tool.name,
                    "input_schema": _schema_structure(tool.input_schema),
                    "output_schema": _schema_structure(tool.output_schema),
                }
                for tool in sorted(remote_tools, key=lambda item: item.name)
            ]
        )
        snapshot = MCPServerSnapshot(
            server_id=server.policy.server_id,
            protocol_version=protocol_version,
            server_name=discovery.server_info.name,
            server_version=discovery.server_info.version,
            capability_hash=capability_hash,
            tool_catalog_hash=tool_catalog_hash,
            mapped_tools=tuple(tool.local_name for tool in mapped),
        )
        return protocol_version, discovery, mapped, snapshot, ttl_ms

    def refresh(
        self,
        server_id: str,
        *,
        force: bool = False,
    ) -> MCPServerSnapshot:
        """刷新能力目录；绑定后的漂移必须由新运行显式处理。"""

        server = self._get_server(server_id)
        if (
            not force
            and server.snapshot is not None
            and self.monotonic() < server.cache_deadline
        ):
            return server.snapshot
        protocol, discovery, mapped, snapshot, ttl_ms = self._load_remote(server)
        if (
            server.bound
            and server.snapshot is not None
            and snapshot != server.snapshot
        ):
            raise MCPCapabilityDriftError(
                f"MCP Server {server_id} 能力在工具绑定后发生变化"
            )
        server.protocol_version = protocol
        server.discovery = discovery
        server.mapped_tools = mapped
        server.snapshot = snapshot
        server.cache_deadline = (
            self.monotonic() + ttl_ms / 1000 if ttl_ms is not None else 0.0
        )
        return snapshot

    def validate_snapshot(
        self,
        snapshot: MCPServerSnapshot,
    ) -> MCPServerSnapshot:
        """恢复执行前强制重新发现，拒绝远程版本或目录漂移。"""

        server = self._get_server(snapshot.server_id)
        _protocol, _discovery, _mapped, current, _ttl_ms = self._load_remote(
            server
        )
        if current != snapshot:
            raise MCPCapabilityDriftError(
                f"MCP Server {snapshot.server_id} 与 Checkpoint 快照不一致"
            )
        return current

    def register_tools(
        self,
        registry: ToolRegistry,
        server_id: str,
    ) -> MCPServerSnapshot:
        """把审核后的 MCP 工具注册为本地 JSON Schema 工具。"""

        server = self._get_server(server_id)
        snapshot = self.refresh(server_id)
        if server.bound:
            raise MCPGatewayError(f"MCP Server 已绑定工具：{server_id}")
        for mapped in server.mapped_tools:
            definition = ToolDefinition(
                name=mapped.local_name,
                description=mapped.description,
                access=mapped.access,
                executes_project_code=mapped.executes_project_code,
                requires_explicit_authorization=(
                    mapped.requires_explicit_authorization
                ),
            )

            def handler(
                arguments: Mapping[str, Any],
                *,
                current_server_id: str = server_id,
                remote_name: str = mapped.remote_name,
            ) -> ToolResult[Any]:
                """把 Registry 调用转发给指定远程 MCP 工具。"""

                return self.call_tool(
                    current_server_id,
                    remote_name,
                    arguments,
                )

            registry.register_json_schema(
                definition,
                mapped.input_schema,
                handler,
            )
        server.bound = True
        return snapshot

    def _record_audit(
        self,
        server_id: str,
        remote_name: str,
        status: str,
        private_meta: Mapping[str, Any] | None = None,
    ) -> None:
        meta_hash = (
            _stable_hash(dict(private_meta)) if private_meta else None
        )
        self._audit.append(
            MCPAuditEvent(
                server_id=server_id,
                remote_name=remote_name,
                status=status,
                private_meta_hash=meta_hash,
            )
        )

    def _normalize_complete_result(
        self,
        server_id: str,
        mapped: MCPMappedTool,
        result: MCPCompleteToolResult,
    ) -> ToolResult[Any]:
        """剥离私有元数据、限制结果规模并验证结构化输出。"""

        if len(result.content) > self.config.max_content_blocks:
            raise MCPResultValidationError("MCP 工具内容块数量超过上限")
        public_content = _strip_private_meta(result.content)
        structured = _strip_private_meta(result.structured_content)
        public_data = {
            "content": public_content,
            "structured_content": structured,
        }
        if _json_size(public_data) > self.config.max_result_bytes:
            raise MCPResultValidationError("MCP 工具结果超过大小上限")
        if mapped.output_schema is not None:
            if structured is None:
                raise MCPResultValidationError(
                    f"MCP 工具缺少结构化输出：{mapped.remote_name}"
                )
            try:
                validator_type = validator_for(mapped.output_schema)
                validator_type(mapped.output_schema).validate(structured)
            except JSONValidationError as exc:
                raise MCPResultValidationError(
                    f"MCP 工具输出不满足审核 Schema：{exc.message}"
                ) from exc
        if result.is_error:
            self._record_audit(
                server_id,
                mapped.remote_name,
                "tool_error",
                result.private_meta,
            )
            return ToolResult.failure(
                ToolErrorKind.EXECUTION_ERROR,
                f"远程 MCP 工具报告业务错误：{mapped.remote_name}",
                data=public_data,
                metadata={
                    "server_id": server_id,
                    "remote_name": mapped.remote_name,
                },
            )
        self._record_audit(
            server_id,
            mapped.remote_name,
            "completed",
            result.private_meta,
        )
        return ToolResult.success(
            public_data,
            metadata={
                "server_id": server_id,
                "remote_name": mapped.remote_name,
            },
        )

    def _handle_input_required(
        self,
        server_id: str,
        mapped: MCPMappedTool,
        arguments: Mapping[str, Any],
        result: MCPInputRequiredResult,
    ) -> ToolResult[Any]:
        """保存待输入状态，但不允许模型代替用户自动回答。"""

        pending_id = uuid4().hex
        self._pending[pending_id] = MCPPendingInput(
            pending_id=pending_id,
            server_id=server_id,
            remote_name=mapped.remote_name,
            arguments=dict(arguments),
            input_requests=result.input_requests,
            request_state=result.request_state,
        )
        self._record_audit(
            server_id,
            mapped.remote_name,
            "input_required",
            result.private_meta,
        )
        return ToolResult.failure(
            ToolErrorKind.INPUT_REQUIRED,
            "远程 MCP 工具需要宿主向用户收集额外输入",
            details={
                "pending_id": pending_id,
                "request_ids": sorted(result.input_requests),
            },
            metadata={
                "server_id": server_id,
                "remote_name": mapped.remote_name,
            },
        )

    def _mapped_tool(
        self,
        server: _AttachedServer,
        remote_name: str,
    ) -> MCPMappedTool:
        for mapped in server.mapped_tools:
            if mapped.remote_name == remote_name:
                return mapped
        raise MCPGatewayError(f"远程工具未获宿主授权：{remote_name}")

    def call_tool(
        self,
        server_id: str,
        remote_name: str,
        arguments: Mapping[str, Any],
    ) -> ToolResult[Any]:
        """调用已映射工具，并区分协议失败、业务失败和待用户输入。"""

        server = self._get_server(server_id)
        try:
            self.refresh(server_id)
            mapped = self._mapped_tool(server, remote_name)
            try:
                validator_type = validator_for(mapped.input_schema)
                validator_type(mapped.input_schema).validate(dict(arguments))
            except JSONValidationError as exc:
                self._record_audit(
                    server_id,
                    remote_name,
                    "invalid_arguments",
                )
                return ToolResult.failure(
                    ToolErrorKind.INVALID_ARGUMENT,
                    f"MCP 工具参数不满足宿主审核 Schema：{remote_name}",
                    details={
                        "path": [str(item) for item in exc.absolute_path],
                        "validator": exc.validator,
                        "message": exc.message,
                    },
                )
            result = server.port.call_tool(
                self._context(server.protocol_version or ""),
                name=remote_name,
                arguments=dict(arguments),
                timeout_seconds=server.policy.request_timeout_seconds,
            )
            if isinstance(result, MCPInputRequiredResult):
                return self._handle_input_required(
                    server_id,
                    mapped,
                    arguments,
                    result,
                )
            return self._normalize_complete_result(
                server_id,
                mapped,
                result,
            )
        except TimeoutError:
            self._record_audit(server_id, remote_name, "timeout")
            return ToolResult.failure(
                ToolErrorKind.TIMEOUT,
                f"MCP 工具调用超时：{remote_name}",
                retryable=True,
            )
        except (
            MCPGatewayError,
            JSONSchemaError,
            JSONValidationError,
            TypeError,
            ValueError,
        ) as exc:
            self._record_audit(server_id, remote_name, "protocol_error")
            return ToolResult.failure(
                ToolErrorKind.PARSE_ERROR,
                f"MCP 工具结果或能力校验失败：{remote_name}",
                details={
                    "exception_type": type(exc).__name__,
                    "message": str(exc),
                },
            )
        except Exception as exc:
            self._record_audit(server_id, remote_name, "protocol_error")
            return ToolResult.failure(
                ToolErrorKind.INTERNAL_ERROR,
                f"MCP 传输或 SDK 适配器失败：{remote_name}",
                retryable=True,
                details={
                    "exception_type": type(exc).__name__,
                    "message": str(exc),
                },
            )

    def resume_tool(
        self,
        pending_id: str,
        input_responses: Mapping[str, Any],
    ) -> ToolResult[Any]:
        """由宿主收集用户输入后携带 requestState 重试原工具。"""

        try:
            pending = self._pending[pending_id]
        except KeyError as exc:
            raise MCPGatewayError(f"未知 MCP pending_id：{pending_id}") from exc
        expected = set(pending.input_requests)
        provided = set(input_responses)
        if expected != provided:
            raise MCPGatewayError(
                f"MCP 输入响应键不匹配；需要={sorted(expected)}，"
                f"实际={sorted(provided)}"
            )
        server = self._get_server(pending.server_id)
        mapped = self._mapped_tool(server, pending.remote_name)
        try:
            result = server.port.call_tool(
                self._context(server.protocol_version or ""),
                name=pending.remote_name,
                arguments=pending.arguments,
                timeout_seconds=server.policy.request_timeout_seconds,
                input_responses=dict(input_responses),
                request_state=pending.request_state,
            )
            del self._pending[pending_id]
            if isinstance(result, MCPInputRequiredResult):
                return self._handle_input_required(
                    pending.server_id,
                    mapped,
                    pending.arguments,
                    result,
                )
            return self._normalize_complete_result(
                pending.server_id,
                mapped,
                result,
            )
        except TimeoutError:
            self._record_audit(
                pending.server_id,
                pending.remote_name,
                "timeout",
            )
            return ToolResult.failure(
                ToolErrorKind.TIMEOUT,
                f"MCP 工具重试超时：{pending.remote_name}",
                retryable=True,
            )
        except (
            MCPGatewayError,
            JSONSchemaError,
            JSONValidationError,
            TypeError,
            ValueError,
        ) as exc:
            self._record_audit(
                pending.server_id,
                pending.remote_name,
                "protocol_error",
            )
            return ToolResult.failure(
                ToolErrorKind.PARSE_ERROR,
                f"MCP 工具重试结果校验失败：{pending.remote_name}",
                details={
                    "exception_type": type(exc).__name__,
                    "message": str(exc),
                },
            )
        except Exception as exc:
            self._record_audit(
                pending.server_id,
                pending.remote_name,
                "protocol_error",
            )
            return ToolResult.failure(
                ToolErrorKind.INTERNAL_ERROR,
                f"MCP 工具重试传输失败：{pending.remote_name}",
                retryable=True,
                details={
                    "exception_type": type(exc).__name__,
                    "message": str(exc),
                },
            )
