# 模块 11 面试讲解：现代 MCP Gateway

## 30 秒回答

我把 RepoAgent 作为 MCP Host，Gateway 是 Host 内的 Client，外部服务是 Server。Gateway 先用 `server/discover` 和分页 `tools/list` 获取能力，但远程声明不会直接注册：只有 Host Policy 明确审核的工具才会映射到本地 Tool Registry，远程 description 和 annotations 不可信，input/output Schema 还要与审核版本核对。调用时 Registry 和 Gateway 双重校验参数，结果区分协议错误、工具业务错误和 InputRequired，`_meta` 不进入模型。Checkpoint 保存 Server 能力与工具目录哈希，恢复时拒绝静默漂移。

## 2 分钟回答

MCP 解决的是 LLM 应用与外部数据和工具之间的标准化互操作，不负责 Agent 的规划、记忆和权限。我项目里的 Host 是 RepoAgent，它管理模型、ReAct、上下文和用户授权；MCPGateway 是 Client；问题跟踪器、数据库等是 Server。

我按 2026-07-28 现代协议实现。它与很多旧教程不同：现代版本取消了核心协议的 initialize/initialized 会话握手，版本、clientInfo 和 clientCapabilities 每个请求都带；server/discover 用于预先获得 Server 支持版本和能力。Streamable HTTP 请求还带 MCP-Protocol-Version、Mcp-Method，调用工具时带 Mcp-Name。

Server 声明的能力不等于 Host 授权。每个外部工具都有 MCPToolPolicy，包含 Host 编写的本地名称、中文描述、审核 Schema、read/execute 风险和是否需要确认。未审核远程工具不注册；远程 description、instructions 和 annotations 不进入模型。工具名称用 Host 配置的 server_id 做命名空间，因为 serverInfo.name 不保证全局唯一。

Registry 在调用前按 JSON Schema 校验，Gateway 直调也再校验。结果把 content 和 structuredContent 作为不可信 Evidence，递归剥离 `_meta`；`isError=true` 是工具业务错误，HTTP 或 JSON-RPC 失败才是协议错误。InputRequired 会暂停并交给 Host 向用户收集信息，模型不能替用户确认。

为了防止长任务恢复时远程 Server 已升级，我保存协议版本、Server 版本、capability hash 和 tool catalog hash。恢复前强制重新发现；不一致就重新规划，而不是让旧计划调用新 Schema。

## MCP 不是什么

- MCP 不是 Agent 框架：不提供 Plan/ReAct/Reflection。
- MCP 不是权限系统：不能替代用户授权和 Host Policy。
- MCP 不是 RAG：它可以提供 Resource，但不决定检索、分块和排序。
- MCP 不是 Skill：它可以传输 Skill 扩展，但 Skill 仍是流程知识。
- MCP 不是某一种传输：stdio 和 Streamable HTTP 都可以承载协议。

## 核心概念对比

| 概念 | 解决的问题 | 在本项目中的位置 |
|---|---|---|
| MCP Host | 管理模型、Client、权限和上下文 | RepoAgent |
| MCP Client | 与一个 Server 通信 | Gateway/HTTP Adapter |
| MCP Server | 暴露 Tools/Resources/Prompts | 外部服务或 Registry Adapter |
| MCP Tool | 模型可请求执行的函数 | 映射到 Tool Registry |
| MCP Resource | 可读取的上下文数据 | 未来进入 Untrusted Evidence |
| MCP Prompt | 用户可选择的消息模板 | 未来作为用户侧模板 |
| MCP Extension | 双方显式协商的扩展 | 尚未启用 |

## 面试官可能追问

### MCP 为什么需要 Host、Client、Server 三个角色？

Host 是完整 AI 应用，持有模型、权限、UI 和多个连接；Client 是 Host 内针对某个 Server 的协议连接或请求端；Server 提供能力。一个 Host 可以同时管理多个 Client，Server 不应直接控制 Host 的模型或权限。

### 2026-07-28 与旧 MCP 最大区别是什么？

现代版本把核心协议改成无会话、每请求自包含。旧版在连接开始执行 initialize/initialized 并可能使用 Mcp-Session-Id；现代请求每次携带协议版本、Client 身份和能力，Server 通过 server/discover 声明支持版本与能力。应用级状态应使用显式 handle，而不是隐藏连接会话。

### 为什么无会话更适合远程部署？

任何 Server 实例都可以处理单次请求，不依赖 sticky session 或共享协议会话存储，适合普通负载均衡、无状态扩缩容和失败重试。状态若确实存在，应作为工具参数或 Server 返回的显式 handle。

### server/discover 是不是每次都必须调用？

现代 Server 必须实现，Client 可以提前调用做版本和能力发现，也可以直接请求并处理 UnsupportedProtocolVersion。当前 Gateway 为了生成可恢复能力快照，选择显式 discover。

### MCP 的 Tools、Resources、Prompts 有什么区别？

Tool 是模型控制的可执行动作；Resource 是应用或模型读取的数据；Prompt 是用户控制的模板化消息。当前项目只接 Tool，因为 ReAct 已有 Tool Registry。Resource 接入时应作为不可信 Evidence，Prompt 也不能自动提升为 system instruction。

### 为什么不能 list_tools 后全部交给模型？

远程 Server 可能新增高风险工具，也可能被替换。发现只说明“远程声称它存在”，不是当前用户授权。Host 必须逐工具审核并应用最小权限。

### 远程 annotations 有什么用，为什么不信任？

readOnlyHint、destructiveHint 等可以帮助 UI 和策略判断，但本质是 Server 自报。恶意或错误 Server 可以谎报，所以当前风险等级完全由 Host Policy决定。

### 为什么远程 description 也不直接使用？

Tool description 会进入模型上下文，远程内容可以携带 Prompt injection。当前只用 Host 审核的中文 description；远程 description 只保留在发现对象，不进入 Registry。

### inputSchema 为什么还要本地审核和复验？

Schema 决定模型能生成哪些参数，也决定远程调用的数据边界。Server 新增 admin、path 或 command 参数会显著改变风险。Gateway 比较审核结构，调用前用 JSON Schema 再验证，避免漂移和非法参数出网。

### outputSchema 有什么价值？

它让程序不必从自由文本解析结果，也能检测 Server 返回结构是否变化。当前 structuredContent 存在审核 outputSchema 时必须再次验证；content 仍保留用于模型阅读。

### 为什么拒绝 `$ref` 和 `pattern`？

`$ref` 可能触发本地或远程引用解析，需要单独的 URI 和网络边界；复杂 `pattern` 可能造成正则拒绝服务。第一版选择受控子集，生产系统可以加入离线引用解析和安全正则实现。

### 多个 Server 都有 search 工具怎么办？

MCP 只保证名称在单个 Server 内唯一，serverInfo.name 也不保证全局唯一。我使用 Host 配置的 stable server_id 生成 `mcp__server__tool` 本地名，并允许 Policy 显式指定别名。

### `isError=true` 和 JSON-RPC error 有什么区别？

JSON-RPC error 表示方法、协议、参数解析或 Server 基础设施层失败；CallToolResult 的 isError 表示协议调用成功完成，但工具业务失败，例如权限不足。两者的重试、反思和用户提示策略不同。

### `_meta` 为什么不交给模型？

`_meta` 用于 Client/Host 私有数据，例如内部 request id、UI 信息或追踪字段。模型只需要 content 和 structuredContent。当前 Gateway 递归剥离 `_meta`，只在审计日志保存哈希。

### InputRequired 怎么处理？

Server 返回 inputRequests 和可选 requestState。Gateway 生成 pending_id，Host 向用户展示表单或确认，之后使用新的 JSON-RPC id、inputResponses 和原 requestState 重试。模型不能自动替用户选择 accept。

### stdio 和 Streamable HTTP 怎么选？

stdio 适合本地子进程，部署简单、边界是进程启动和 stdin/stdout；Streamable HTTP 适合远程服务，需要 HTTPS、认证、超时、重试和网关策略。当前代码实现现代 JSON HTTP 和内存 Server，stdio 留给官方 SDK v2 适配。

### MCP 如何做认证？

远程 HTTP 通常结合 OAuth 2.x、受保护资源元数据、scope 和 Token Store。认证证明调用者身份，授权仍要同时满足 Server scope 与 Host 当前工具策略。Token 不能进入 Prompt、ToolResult 或 Checkpoint。

### 为什么能力变化不热更新？

当前计划、Skill 和模型工具 Schema都基于旧目录。热更新会让同一次 run 前后工具语义不一致。当前绑定后检测到目录变化就停止，创建新 Registry 并重新规划。

### TTL 缓存和 listChanged 怎么理解？

现代列表结果可用 ttlMs/cacheScope 表达缓存。最新规范还通过订阅流统一处理变化通知。当前实现 TTL，未实现 SSE subscriptions；恢复或过期后会主动重新发现。

### MCP 和 Skill 如何组合？

Skill 可以说明何时、按什么顺序使用 MCP Tool；MCP Skills Extension 也可以分发 Skill。但远程分发不等于安装可信，仍要进入 Skill 的审核、版本、hash 和可信根流程。

### MCP Resource 与现有 RAG 怎么组合？

Resource 提供标准化读取接口，RAG 负责导入、分块、Embedding、过滤和排名。可以把 MCP Resource 作为 RAG 数据源，或短资源直接转成带 Server/URI 引用的 Untrusted Evidence Packet。

## 一条完整口述链路

用户要查询外部工单。Gateway 先带现代每请求元数据调用 server/discover，选择共同协议版本，再分页获取 tools/list。远程 search 工具只有在 Host Policy 中有审核记录才映射为本地 `mcp__issue_tracker__search_issues`。模型看到的是 Host 描述和审核 Schema。ReAct 请求调用后，Registry 检查当前步骤权限并校验参数，Gateway 再校验，然后 HTTP Adapter 发送带 MCP-Protocol-Version、Mcp-Method 和 Mcp-Name 的 tools/call。结果剥离 `_meta`、限制大小、校验 outputSchema，再作为 Untrusted Evidence 返回 ReAct。若 Server 要用户确认，调用进入 InputRequired，由 Host 收集输入后重试。

## 当前代码证据

- `mcp/models.py`：协议身份、能力、Tool、Policy、Snapshot、InputRequired。
- `mcp/port.py`：SDK 与传输无关的 Client Port。
- `mcp/gateway.py`：发现、分页、Policy、Schema、Registry、结果与恢复。
- `mcp/http.py`：2026-07-28 现代 JSON Streamable HTTP。
- `mcp/server.py`：Tool Registry 的内存 MCP Server 适配器。
- `mcp/fakes.py`：确定性记录 Client。
- `tools/registry.py`：外部 JSON Schema 工具注册和调用前校验。
- `tests/test_mcp_gateway.py`：Gateway 安全和恢复测试。
- `tests/test_mcp_http.py`：真实 HTTP JSON-RPC 线协议测试。

全项目当前 182 个测试，179 个通过，3 个按环境条件跳过。

## 主动说明的局限

1. 没有 stdio 和官方 Python SDK v2 适配器。
2. 不兼容 initialize/session 旧协议。
3. HTTP 暂不处理 SSE 与订阅。
4. 没有 OAuth 和 Token 生命周期。
5. Resources、Prompts 和 Extensions 尚未接入。
6. InputRequired 尚未结合 LangGraph interrupt 持久化。
7. 当前 ReAct 是同步调用，不适合大量并发 Server。
8. 没有真实第三方 Server 契约测试。
