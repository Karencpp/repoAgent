# ADR-011：现代 MCP Gateway、宿主策略与能力漂移校验

- 状态：Accepted
- 日期：2026-07-31
- 模块：Model Context Protocol

## 背景

RepoAgent 已经有本地 Tool Registry、ReAct、Skill、RAG、Memory 和 Context Builder。要接入问题跟踪器、数据库、浏览器或其他外部能力，需要一个标准化协议边界，而不是为每个系统单独编写模型工具格式。

MCP 解决 Host 与外部 Server 之间的互操作，但协议本身不替宿主完成：

- 工具可信度判断；
- 用户授权和最小权限；
- Prompt injection 防护；
- 多 Server 工具名称冲突；
- Checkpoint 恢复时的能力一致性；
- 结果大小和上下文预算控制。

同时，MCP `2026-07-28` 相比 2025 年版本有较大变化：核心协议改为无会话、每请求自包含；`initialize/initialized` 不再是现代版本的主路径，版本、Client 身份和能力改为每个请求的 `_meta`，Server 提供 `server/discover`。

## 决策

### 1. 明确 Host、Client、Server 三层

```text
RepoAgent = MCP Host
MCPGateway / HTTP Adapter = Host 内的 MCP Client
Issue Tracker / Database / Registry Adapter = MCP Server
```

Host 管理模型、上下文、权限、用户同意和多个 Client。每个 Client 连接或请求一个 Server。Server 提供 Tools、Resources、Prompts 或扩展能力。

MCP 不是 Agent 框架：它不负责 ReAct、规划、记忆或反思；它只标准化外部上下文和能力怎样被发现与调用。

### 2. 核心层依赖 MCPClientPort，不绑定正在迁移的 SDK API

`MCPClientPort` 暴露：

- `discover`；
- `list_tools`；
- `call_tool`。

Gateway 只依赖这个端口。当前提供：

- `ModernHTTPMCPClient`：真实的现代 JSON Streamable HTTP 适配器；
- `RegistryMCPServer`：把现有 Tool Registry 暴露成协议无关的内存 Server；
- `RecordingMCPClient`：确定性测试 Client。

Python SDK 正处在 v1/v2 与新旧协议切换窗口。端口隔离让后续接入官方 stdio 或 SDK v2 时，不需要改权限、Schema、结果和恢复逻辑。

### 3. 使用 2026-07-28 每请求元数据

每次 discover、list 和 call 都重新携带：

```text
io.modelcontextprotocol/protocolVersion
io.modelcontextprotocol/clientInfo
io.modelcontextprotocol/clientCapabilities
```

HTTP 适配器同时发送：

```text
MCP-Protocol-Version
Mcp-Method
Mcp-Name（tools/call）
```

请求不发送旧版 `Mcp-Session-Id`。`server/discover` 返回支持版本、Server 身份和能力，Gateway 选择宿主策略中第一个共同版本；没有交集时在列工具前失败。

### 4. Server 声明能力，Host 决定是否采用

远程返回的 tool name、description、annotations、inputSchema 和 outputSchema 都是 Server 声明，不自动成为宿主权限。

每个可暴露工具必须有 `MCPToolPolicy`：

- remote_name；
- 稳定 local_name 或本地命名规则；
- 宿主编写的 description；
- 宿主审核的 input/output Schema；
- read 或 execute；
- 是否执行目标项目代码；
- 是否需要显式授权。

未出现在 policy 的远程工具被忽略。远程 description、instructions 和 annotations 不进入 Tool Registry。

### 5. Schema 采用“发现结果核对审核版本”

远程 inputSchema 必须与宿主 Policy 中审核过的 Schema 结构一致。比较时忽略 description、title、examples 等展示注解，但保留真正影响输入结构和校验的字段。

因此：

- 远程恶意说明不会进入模型工具定义；
- 新增参数、修改类型或 required 会触发能力漂移；
- Registry 调用前用 JSON Schema 再校验一次参数；
- 直接调用 Gateway 也会再次校验；
- outputSchema 存在时，返回 structuredContent 还会被宿主复验。

当前拒绝 `$ref` 和 `pattern`。前者需要额外的引用解析边界，后者可能造成正则拒绝服务。项目只支持受控 JSON Schema 子集。

### 6. 本地名称不依赖 serverInfo.name

MCP 规范只保证工具名在单个 Server 内唯一，`serverInfo.name` 本身也不保证全局唯一。

默认映射：

```text
mcp__<host-configured-server-id>__<normalized-remote-name>
```

`server_id` 来自宿主配置，不能由远程 Server 自己决定。名称冲突或超过本地 ToolCall 长度限制时显式失败，也支持 Policy 直接给出 local_name。

### 7. 目录分页、缓存和漂移都是显式状态

Gateway 遍历 `tools/list` 分页并限制：

- 最大页数；
- 最大工具数量；
- 重复工具名；
- 循环 cursor。

`ttlMs` 决定本地目录缓存时间。`cacheScope` 保留在协议模型中，当前每个 Gateway 实例只服务一个宿主上下文，不跨用户共享缓存。

发现后生成 `MCPServerSnapshot`：

- server_id；
- protocol_version；
- Server name/version；
- capability hash；
- tool catalog hash；
- mapped local tools。

工具绑定到 Registry 后目录发生变化时不热替换；当前 run 直接报漂移。Checkpoint 恢复前强制重新 discover/list 并比较 Snapshot。

### 8. 工具业务错误与协议错误分开

```text
HTTP / JSON-RPC / 解析失败
  → protocol or infrastructure error

tools/call 成功返回，但 isError=true
  → remote tool business/execution error

本地参数不满足审核 Schema
  → invalid_argument，不发送远程请求

远程超时
  → timeout，可重试
```

这与本地 pytest 模块的原则一致：通信成功不等于业务成功，业务失败也不等于协议断开。

### 9. `_meta` 不进入模型观察

MCP 工具结果可能同时包含：

- content：供模型读取；
- structuredContent：供程序与模型使用；
- `_meta`：只供 Client/Host 使用。

Gateway 递归移除公开结果中的 `_meta`，顶层 private_meta 只生成审计哈希。ToolResult 只携带 Host 允许暴露的 content、structured content、server_id 和 remote_name。

### 10. InputRequired 由 Host 处理

现代 MCP 可以返回 `input_required`。Gateway：

1. 保存 inputRequests、requestState 和原参数；
2. 返回 `ToolErrorKind.INPUT_REQUIRED`；
3. 不允许模型自己回答用户确认；
4. Host 收集输入后调用 `resume_tool`；
5. 使用新 JSON-RPC id，携带 inputResponses 和 requestState 重试。

当前 pending input 保存在进程内，还没有写入 LangGraph interrupt/Checkpoint，这是下一步集成点。

### 11. HTTP 安全基线

`ModernHTTPMCPClient`：

- 远程地址默认必须 HTTPS；
- HTTP 只允许显式开启的 localhost；
- URL 不能嵌入凭据；
- 自定义头不能覆盖 MCP 保留头；
- 不自动跟随重定向；
- 限制响应字节数；
- 当前只接收 JSON，不实现 SSE 流；
- 超时由传输适配器执行。

OAuth、动态 Client Registration 和企业 Token Store 当前未实现。API Token 应通过 Host 的秘密管理注入 HTTP header，不能进入模型上下文或 Checkpoint。

### 12. 当前只接入 Tools

ServerCapabilities 已区分 tools、resources、prompts 和 extensions，但当前 ReAct 的直接消费边界是 Tool Registry，因此模块只实现 Tool 流程。

未来：

- MCP Resource 应转换成 `UNTRUSTED_EVIDENCE` ContextPacket；
- MCP Prompt 是用户选择的模板，不应自动升级成系统指令；
- MCP Skills Extension 应进入现有 Skill 安装、版本和可信根流程；
- Tasks、Apps 等 Extension 必须显式协商，不能看到声明就自动启用。

## 没有选择的方案

### 远程 list_tools 后全部注册

会把 Server 新增能力静默暴露给模型，也无法给每个工具定义本地风险和授权策略。

### 信任远程 readOnlyHint/destructiveHint

annotations 是提示，不是权限事实；恶意 Server 可以把删除操作标成只读。

### 把远程 description 直接交给模型

工具说明可能包含 Prompt injection。当前模型只看到宿主审核的中文 description。

### 使用 serverInfo.name 作为全局命名空间

它不保证唯一，也可能在恢复时被远程修改。宿主配置的 server_id 才是稳定身份。

### 能力变化时热更新当前 Registry

当前计划和模型上下文可能基于旧 Schema，热替换会让同一次 run 前后语义不一致。第一版选择失败并重新开始或重新规划。

### 只依赖 Server 校验工具参数和输出

远程 Server 可能实现错误或被替换。本地按照审核 Schema 复验可以更早失败并保留一致错误类型。

### 把 InputRequired 直接发给模型回答

确认、凭据和用户表单属于 Host/User 边界。模型不能替用户同意删除、支付或授权。

## 当前局限

- 没有官方 Python SDK v2/stdio 适配器；
- HTTP 只支持 JSON 响应，不支持 SSE 和订阅流；
- 不兼容旧版 initialize/session 协议；
- 没有 OAuth、Token 刷新和企业秘密存储；
- 没有 Resources、Prompts、Tasks、Apps 和 Skills Extension；
- InputRequired pending 状态尚未写入 LangGraph Checkpoint；
- 同步 ReAct 限制了高并发远程调用；
- JSON Schema 暂不支持 `$ref`、`pattern` 和远程引用；
- 没有真实第三方 MCP Server 的契约测试。

## 官方依据

- [MCP 2026-07-28 Specification](https://modelcontextprotocol.io/specification/2026-07-28)
- [Versioning and Compatibility](https://modelcontextprotocol.io/specification/2026-07-28/basic/versioning)
- [MCP Tools](https://modelcontextprotocol.io/specification/2026-07-28/server/tools)
- [Streamable HTTP Transports](https://modelcontextprotocol.io/specification/2026-07-28/basic/transports)
- [Official Python SDK](https://github.com/modelcontextprotocol/python-sdk)

## 验证证据

新增 28 个 MCP 测试，覆盖现代 HTTP 线协议、每请求元数据、分页、版本协商、Schema 审核、工具命名空间、参数和结果复验、私有元数据、超时、业务错误、InputRequired、TTL 缓存、能力漂移和 Registry Server 端到端调用。全项目当前 182 个测试，179 个通过，3 个按环境条件跳过。
