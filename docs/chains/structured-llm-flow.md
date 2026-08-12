# 结构化 LLM 调用链路

## 三个模型角色

```text
用户目标 + ProjectContext
  → StructuredPlanner
  → ExecutionPlan
  → LangGraph Execute
       → ReActExecutor
       → StructuredDecisionClient
       → tool_call / final_answer
  → ObjectiveCandidateEvaluator
       → 编译、测试、变更范围事实
  → 失败时 StructuredReflector
       → retry / replan
```

Planner、Decision Client 和 Reflector 可以共享一个 GLMChatClient 连接池，但它们使用不同 Prompt 和不同领域 Schema。共享供应商客户端不等于混合角色职责。

## 单次结构化调用

```text
领域请求
  → 转为有限 JSON 上下文
  → 加入 Prompt injection 边界
  → 加入领域 JSON Schema
  → StructuredJSONRequest
  → GLMChatClient
       ├─ Bearer Header
       ├─ JSON mode
       ├─ thinking disabled
       ├─ timeout
       └─ max_tokens
  → HTTP 响应
  → 最小协议模型校验
  → json.loads
  → 领域 Pydantic 校验
  → Host 权限校验
```

## 错误分类

```text
本地环境变量缺失 → LLMConfigurationError
HTTP 401 / 403      → LLMAuthenticationError
HTTP 429            → LLMRateLimitError
请求超时            → LLMTimeoutError
连接失败            → LLMTransportError
错误状态/协议缺字段  → LLMResponseError
非法 JSON/领域字段   → LLMStructuredOutputError
```

分类不是为了“异常类越多越高级”，而是为了让上层采用不同策略：鉴权错误立即停止；限流可延迟；Schema 错误可修改提示或有限重试；领域越权必须拒绝。

## 双重工具校验

```text
Planner 输出 allowed_tools
  → 必须属于注册表工具名
  → ReAct 只看到当前步骤白名单
  → 模型生成 tool_name + arguments
  → Tool Registry 再校验：
       工具存在
       当前步骤允许
       参数符合 Pydantic Schema
       风险操作已授权
  → 才能执行
```

JSON 合法、计划合法、调用获授权是三个不同结论。

## 真实测试开关

```text
普通 unittest
  → MockTransport
  → 无网络、无密钥、确定性

显式 live test
  → 新 ZHIPUAI_API_KEY
  → RUN_GLM_INTEGRATION=rotated-key-confirmed
  → 一次最小真实请求
```

