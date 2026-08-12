# 模块 07 面试讲解：真实 GLM 与结构化模型边界

## 30 秒回答

我没有把 GLM SDK 直接塞进 LangGraph，而是定义了供应商无关的 StructuredJSONClient。GLM HTTP 适配器负责 Bearer 鉴权、JSON 模式、超时和错误翻译；Planner、ReAct 决策器、Reflector 分别构造自己的 Prompt 和 Pydantic Schema。模型输出要经过 JSON 解析、领域 Schema、工具白名单和 Tool Registry 四层校验。LLM 只负责语义决策，编译、pytest 和变更范围仍由确定性 Evaluator 裁决。普通测试完全离线，真实调用只有轮换密钥后显式开启。

## 2 分钟回答

真实模型接入最容易犯的错误，是让供应商协议侵入所有业务节点，或者认为 response_format=json_object 就等于安全结构化输出。我把边界拆成三层。

第一层是供应商端口。StructuredJSONRequest 只包含消息和目标 Schema；GLMChatClient 才知道智谱地址、Bearer Header、JSON mode、thinking、temperature 和 HTTP 错误码。这样换模型时不改 Planner 和 ReAct。第一版直接用 httpx，是因为这个项目面向面试理解，超时、连接池、401、429 和响应解析都需要看得见。

第二层是角色适配。Planner 生成最多六步的 ExecutionPlan；Decision Client 生成 tool_call 或 final_answer；Reflector 根据客观失败决定 retry 或 replan。三个角色可以共享连接池，但 Prompt 和 Schema 独立。它们只输出简短可审计理由，不保存隐藏思维链。

第三层是确定性控制。JSON mode 只提高语法稳定性，之后还要 Pydantic 校验。Planner 产生的工具名必须属于注册表；ReAct 只看到步骤白名单；真正执行前 Tool Registry 再校验参数和授权。模型即使被源码注释诱导去调用 shell，也无法凭字符串创造能力。

Evaluator 没有改成 LLM，因为编译和测试是可直接计算的事实。Reflection 只能消费 Evaluator 的 rejected 结果，不能把失败“解释成成功”。这保证模型负责模糊语义，程序负责状态、权限和客观真值。

## 面试官可能追问

### 为什么要自己定义端口，不直接使用智谱 SDK？

SDK 适合快速开发，但如果 Planner、Graph 和测试都接触 SDK Response，升级或切供应商会扩大改动面。端口把供应商变化限制在 GLMChatClient。当前用 httpx 还让协议和错误策略更容易讲清楚。生产中可以在端口后换官方 SDK。

### 为什么不用 LangChain 的 ChatModel？

LangChain 可以减少接入代码，但会多一层抽象和版本耦合。本项目已经用 LangGraph 管状态机，模型边界只有一个小接口，直接实现更便于展示依赖倒置。若以后需要多供应商、回调和流式事件，再评估统一 ChatModel。

### 为什么不用 GLM 原生 Function Call？

Function Call 很适合 ReAct，但 Planner 和 Reflector 返回的是领域对象，不是工具调用。第一版用一个 JSON 端口统一三种角色。即使以后 ReAct 改用原生 Function Call，Tool Registry 的本地参数、白名单和授权校验仍不能删除。

### JSON mode 和 Pydantic 各解决什么？

JSON mode 降低 Markdown 或非法 JSON 的概率，解决传输语法；Pydantic 校验必填字段、枚举、长度、步骤数量和额外字段，解决领域契约。工具白名单和授权还在更下一层。

### 为什么 temperature 是 0.1，不是 0？

智谱 OpenAI 兼容文档说明 temperature 的范围是 `(0,1)`，不应假设 0 可用。更重要的是，低 temperature 也不等于确定性，所以普通回归从不依赖真实模型输出。

### 为什么关闭 thinking？

当前节点只需要有限结构化决策，关闭 thinking 可以降低延迟和输出复杂度，也避免系统依赖隐藏推理内容。复杂任务可以按角色配置开启，但业务审计仍只记录简短结论和外部证据。

### 为什么默认选 glm-4.7-flash？

它是官方当前提供的免费文本模型，支持结构化输出和 Agentic Coding，适合面试项目做低成本真实协议验证。默认模型是配置，不是架构决策；需要更强规划能力时通过 `GLM_MODEL` 切换旗舰模型。

### 为什么不让 LLM 做 Evaluator？

pytest 的退出码、编译结果、实际 changed files 都是确定事实。让 LLM 判断会增加成本、波动和误判。LLM Reviewer 可以补充代码风格或设计意见，但不能把客观失败覆盖成通过。

### Prompt injection 怎么防？

Prompt 中把用户目标、仓库内容和工具结果标记为不可信数据，但文字提示只是第一层。真正防线是模型看不到未授权工具、Planner 工具白名单、本地 Registry 校验、路径沙箱和代码执行授权。安全不能只靠一句 system prompt。

### 为什么上下文超限直接失败，不自动截断？

截断可能删除失败测试或权限字段，却不让上层知道，形成错误决策。当前显式失败更诚实。未来可以增加可审计摘要，记录哪些内容被压缩以及摘要对应的 revision。

### 401、429、超时为什么要分开？

它们的恢复策略不同。401 应停止并检查密钥；429 可以等待或换配额；超时可能有限重试；Schema 错误应修改 Prompt 或做有限再生成。一个笼统的“模型失败”无法指导可靠恢复。

### 为什么当前没有自动重试？

重复 LLM 请求会产生额外成本和不同决策，还可能让审计缺少一次真实 attempt。下一步可以只对暂态错误做有上限的指数退避，并记录次数、request id 和成本；鉴权与越权错误绝不盲重试。

### 为什么真实模型测试不能进普通 CI？

它需要网络和密钥，有费用、限流、模型升级和随机波动。普通测试用 MockTransport 精确验证我们控制的协议；live test 只验证当前供应商仍兼容，是显式运行的契约检查。

### 一个 GLM Client 给三个角色，会不会互相污染上下文？

不会。这里共享的是无状态 HTTP 连接池，不是聊天历史。每次请求都显式传入完整消息。角色状态在 LangGraph 和 Checkpoint 中，不能藏在 SDK Client 内。

### API Key 泄露后为什么必须轮换，删消息不行吗？

密钥一旦出现在聊天或日志，就无法证明没有被复制。删除显示内容不等于撤销凭证，唯一可靠措施是服务端吊销旧 Key、生成新 Key，并只通过环境变量注入。

## 本地验证方式

普通回归不需要密钥：

```powershell
python -m unittest discover -s tests -v
```

只有确认旧密钥已吊销并设置新密钥后，才运行一次真实协议检查：

```powershell
$env:ZHIPUAI_API_KEY = "<已轮换的新密钥>"
$env:RUN_GLM_INTEGRATION = "rotated-key-confirmed"
python -m unittest tests.test_glm_live -v
```

## 当前代码证据

- `llm/contracts.py`：供应商无关请求、端口和错误类型。
- `llm/glm.py`：安全配置、HTTP 请求、错误翻译和 JSON 解析。
- `llm/adapters.py`：Planner、ReAct 和 Reflector 的 Prompt 与领域校验。
- `tests/test_glm_adapter.py`：13 个离线协议与领域边界测试。
- `tests/test_glm_live.py`：1 个必须显式解锁的真实接口测试。

全项目当前 100 个测试：99 个通过，1 个真实 GLM 测试默认跳过。

## 主动说明的局限

1. 尚未实现重试、退避、熔断和并发限流。
2. 尚未记录 token usage、request id、延迟和估算成本。
3. JSON Schema 主要通过 Prompt 约束，仍依赖本地 Pydantic 兜底。
4. 字符预算只是 token 预算的近似。
5. ReAct 尚未使用供应商原生 Function Call。
6. Prompt injection 防护不等于 OS 级进程隔离。
7. live test 只检查最小协议，不证明复杂 Agent 效果。

