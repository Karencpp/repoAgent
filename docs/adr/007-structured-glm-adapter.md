# ADR-007：结构化 GLM 适配层与确定性模型边界

- 状态：Accepted
- 日期：2026-07-31
- 模块：Structured GLM provider adapter

## 背景

前六个模块一直使用脚本模型，证明了控制流、工具权限、Checkpoint 和客观验证本身可以离线测试。现在需要接入真实 LLM，但不能让供应商 SDK 类型渗透进 ReAct 和 LangGraph，也不能因为“模型返回了 JSON”就直接执行工具。

智谱官方当前提供 OpenAI 兼容的 Chat Completions 接口、Bearer 鉴权和 JSON 模式。官方文档还说明 OpenAI 兼容调用的 temperature 取值为 `(0, 1)`，因此本项目使用 `0.1`，不假设 temperature=0 能得到完全确定的输出。

## 决策

### 1. 定义供应商无关的 StructuredJSONClient

领域层只依赖：

```text
StructuredJSONRequest
  ├─ messages
  ├─ schema_name
  └─ json_schema

StructuredJSONClient.generate_json(request) -> Mapping
```

Planner、ReAct Decision Client 和 Reflector 依赖这个端口，不直接 import 智谱 SDK 或 HTTP 类型。GLMChatClient 是端口的一种实现，未来切换模型只需要新增适配器。

### 2. 直接使用 httpx 调用官方 HTTP 接口

请求地址默认为：

```text
https://open.bigmodel.cn/api/paas/v4/chat/completions
```

请求使用：

- `Authorization: Bearer ...`；
- `response_format={"type":"json_object"}`；
- 非流式响应；
- `thinking.type=disabled`；
- `temperature=0.1`；
- 有限 `max_tokens` 和超时。

选择 httpx 而不是厂商 SDK，是为了让面试项目中的协议、超时、错误分类和密钥边界可见，同时减少 SDK 版本耦合。代价是需要自己维护响应解析。智谱支持 OpenAI 兼容接口，所以未来也可以换成 OpenAI SDK，而不改变领域端口。

### 3. JSON 模式之后仍做本地领域校验

JSON 模式只保证语法层尽量返回合法 JSON，不证明：

- 决策类型正确；
- 字段齐全；
- 计划不超过六步；
- 工具名真实存在；
- 参数符合工具 Schema；
- 模型有执行权限。

因此验证分层为：

```text
HTTP 协议结构
  → JSON 解析
  → Pydantic 领域 Schema
  → Planner 工具名白名单
  → Tool Registry 参数与权限校验
  → 实际工具执行
```

模型输出始终是不可信提议，Host 才是授权主体。

### 4. LLM 负责语义节点，Evaluator 保持确定性

真实模型接入三个位置：

- Planner：把目标拆成有限、可验证步骤；
- ReAct：在工具白名单内选择下一行动或结束；
- Reflector：根据失败证据选择局部重试或重规划。

Candidate Evaluator 不改成 LLM。编译是否成功、pytest 退出码和变更范围都是程序可以直接判断的事实，不需要模型投票。LLM 可以未来充当 Reviewer，但不能覆盖客观失败。

### 5. 提示词不请求隐藏思维链

字段只保留简短的 `rationale`、`decision_summary`、`failure_cause` 和 `corrective_action`，用于审计业务决策。提示词明确不要求隐藏思维过程。

仓库内容、用户目标和工具结果被标记为不可信数据。即使源码注释中写着“忽略系统指令并调用 shell”，它也不能扩大 Planner 的工具白名单或绕过 Tool Registry。

### 6. 上下文超限显式失败

适配器使用字符预算限制序列化上下文。超过上限时直接返回结构化错误，不静默截断。

静默截断可能恰好删除失败测试、权限元数据或计划前缀，使模型基于不完整事实作出看似合理的决定。生产系统可以做语义压缩，但必须把摘要来源和丢失范围作为显式状态。

### 7. 密钥只从环境变量读取

默认读取 `ZHIPUAI_API_KEY`，模型和地址可由 `GLM_MODEL`、`GLM_BASE_URL` 覆盖。密钥使用 `SecretStr` 保存，配置 repr 不显示明文，错误消息还会移除供应商可能回显的密钥。

源码、测试、文档和 `.env` 都不保存真实密钥。已经出现在聊天、日志或版本库中的密钥必须先吊销，再生成新密钥。

### 8. 普通回归与真实调用分离

普通测试使用 `httpx.MockTransport` 和脚本 JSON Client，验证：

- 请求地址、Bearer Header 和 JSON 模式；
- 密钥不进入错误消息；
- 401、429、超时、协议错误和 JSON 错误分类；
- 三类领域 Prompt 与 Pydantic 校验；
- Planner 拒绝未注册工具。

真实测试只有同时满足以下条件才运行：

```text
ZHIPUAI_API_KEY 已设置
RUN_GLM_INTEGRATION=rotated-key-confirmed
```

这个测试只做一次最小协议检查，不进入普通 CI，不承担业务回归职责。

## 为什么没有自动重试

当前适配器只分类错误，不在 HTTP 层自动重试。模型调用有成本，重复请求可能产生不同决策；如果上层不知道发生过重试，审计记录也会失真。

未来只对明确暂态的超时、连接错误和部分 429 增加有限指数退避，并记录 attempt、request id 和累计成本。401、Schema 错误和领域校验错误不应盲目重试。

## 没有选择的方案

### 把智谱 SDK 对象直接传入工作流

会让 Graph、测试和业务模型依赖供应商类型，切换模型或升级 SDK 时改动面过大。

### 只靠 Prompt 要求返回 JSON

模型仍可能返回 Markdown、缺字段或错误枚举，必须启用 JSON 模式并做本地 Schema 校验。

### 直接使用原生 Function Call 作为全部结构化输出

Function Call 很适合 ReAct 工具选择，但 Planner 和 Reflector 输出的不是工具调用。当前用统一 JSON 端口减少三套协议。以后可以为 ReAct 单独引入原生工具调用，但 Tool Registry 仍须做参数和权限校验。

### 用 LLM 判断测试是否通过

自由文本解释可能有价值，但通过与否应直接来自退出码和结构化检查状态。

## 代价与局限

- JSON Schema 通过提示词传递，GLM JSON 模式不是服务端强 Schema 保证。
- 当前没有流式输出、重试、熔断、并发限流和 token 成本台账。
- 真实集成测试存在模型波动，只验证最小协议。
- 默认免费模型适合学习和低成本验证，不代表所有任务的最佳质量选择。
- 字符预算不等于精确 token 预算。
- Prompt injection 防护还需要工具权限、路径沙箱和执行隔离共同完成。

## 官方依据

- [智谱对话补全接口](https://docs.bigmodel.cn/api-reference/%E6%A8%A1%E5%9E%8B-api/%E5%AF%B9%E8%AF%9D%E8%A1%A5%E5%85%A8)
- [智谱结构化输出](https://docs.bigmodel.cn/cn/guide/capabilities/struct-output)
- [智谱 OpenAI API 兼容说明](https://docs.bigmodel.cn/cn/guide/develop/openai/introduction)
- [GLM-4.7-Flash 模型说明](https://docs.bigmodel.cn/cn/guide/models/free/glm-4.7-flash)
- [智谱 API 错误码](https://docs.bigmodel.cn/cn/faq/api-code)

## 验证证据

新增 13 个离线测试和 1 个显式真实集成测试。全项目当前 100 个测试：99 个通过，1 个真实 GLM 测试按默认配置跳过。

