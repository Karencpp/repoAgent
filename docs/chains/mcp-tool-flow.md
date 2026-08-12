# MCP Tool 从发现到 ReAct 的完整链路

## 架构角色

```text
RepoAgent Host
  ├─ LLM / Planner / ReAct
  ├─ Context Builder
  ├─ Tool Registry
  └─ MCPGateway
       ├─ Client A → Issue Tracker MCP Server
       ├─ Client B → Database MCP Server
       └─ Client C → Browser MCP Server
```

## 现代能力发现

```text
Host MCPServerPolicy
  → server/discover
       每请求携带 protocolVersion
       clientInfo
       clientCapabilities
  → 选择共同协议版本
  → 校验 tools capability
  → tools/list 分页
       ├─ max pages
       ├─ max tools
       ├─ duplicate name
       ├─ cursor cycle
       └─ ttlMs
  → 远程目录
  → 与 Host Policy 对照
       ├─ 未审核工具忽略
       ├─ Schema 结构漂移拒绝
       ├─ 远程 description/annotations 丢弃
       └─ 生成稳定 local_name
  → MCPServerSnapshot
```

## Tool Registry 映射

```text
MCPToolPolicy
  ├─ 宿主 description
  ├─ 审核 input/output Schema
  ├─ access
  ├─ executes_project_code
  └─ explicit authorization
          ↓
ToolDefinition + register_json_schema
          ↓
Model 只看到本地审核版本
```

## 单次工具调用

```text
模型返回 local tool call
  → ReAct 预算与重复调用检测
  → Tool Registry 当前步骤白名单
  → 本地 JSON Schema 参数校验
  → MCPGateway 再次参数校验
  → tools/call
       HTTP headers:
       MCP-Protocol-Version
       Mcp-Method=tools/call
       Mcp-Name=<remote name>
  → MCP Server
  → CallToolResult
       ├─ complete
       └─ input_required
```

## 完成结果

```text
complete
  → 限制 block 数和总字节
  → 递归剥离 _meta
  → outputSchema 复验
  → isError?
       ├─ false → ToolResult.success
       └─ true  → EXECUTION_ERROR
  → ReAct Observation
  → Context Builder 的 UNTRUSTED_EVIDENCE
```

## 多轮输入

```text
input_required
  → Gateway 保存：
       pending_id
       arguments
       inputRequests
       requestState
  → INPUT_REQUIRED
  → Host UI 向用户收集输入
  → resume_tool(pending_id, inputResponses)
  → 新 JSON-RPC id 重试原 tools/call
```

模型不会自动替用户确认。

## 恢复一致性

```text
首次运行
  → capability hash
  → tool catalog hash
  → server name/version
  → protocol version
  → MCPServerSnapshot

Checkpoint 恢复
  → 强制 discover + list
  → 重新计算 Snapshot
  → 完全一致：继续
  → 不一致：MCPCapabilityDriftError
```

