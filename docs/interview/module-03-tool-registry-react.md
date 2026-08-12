# 模块 03 面试讲解：Tool Registry、结构化输出与 ReAct

## 30 秒回答

这一模块把已有仓库工具接进了最小 ReAct Runtime。Tool Registry 将 Pydantic 参数模型转换成 JSON Schema 供模型发现，同时在 Host 端再次做工具白名单和参数校验。模型每轮只能返回 `tool_call` 或 `final_answer` 两种结构化决策；Host 执行工具，把 ToolResult 作为 Observation 放进下一轮。循环有迭代、工具次数、连续错误和重复调用四类停止条件，并用脚本模型做确定性测试。Trace 记录简短决策摘要、Action、Observation 和停止原因，不依赖完整思维链。

## 2 分钟回答

ReAct 本质是“根据当前目标和观察做决策，采取动作，拿到新观察，再决策”的循环；Function Calling 是其中 Action 的结构化表达方式。模型并不执行函数，Host 才是控制平面。

我先用 Pydantic 为五个工具建立模型参数边界。Registry 向模型提供工具名称、中文说明、JSON Schema 和风险元数据。模型返回工具名和参数后，Registry 还会再次检查工具白名单并校验参数，校验通过才转换成内部 dataclass 调用领域工具。这样即使模型生成不存在的工具、额外字段或越界参数，也不会穿透执行边界。

模型决策也使用带判别字段的联合 Schema，只允许工具调用和最终回答。每次工具结果会形成可审计事件，并作为结构化 Observation 加入下一轮。单次工具错误不会立刻判整个任务失败，因为模型可能修正参数；连续错误到达上限才停止。

循环不能只靠 Prompt 控制，所以 Runtime 在执行前检查工具预算和调用指纹，避免越界和重复副作用；迭代预算控制总成本。为了让测试稳定，我没有直接依赖真实 LLM，而是用脚本模型精确模拟“先搜索、再回答”“连续错误”“重复调用”等路径。未来真实模型只需实现 RawDecisionClient，核心循环不用改变。

## 一条必须讲透的逻辑链

```text
Goal
  → ModelRequest(工具 Schema + 历史 Observation + 剩余预算)
  → 模型原始 JSON
  → 决策 Schema 校验
  → tool_call
  → 预算/重复检查
  → Registry 白名单 + 参数 Schema
  → Repository Tool
  → ToolResult
  → ReActEvent
  → 下一轮 Observation
  → final_answer 或确定性停止
```

## 三个概念不要混淆

### ReAct

一种 Agent 循环模式：决策、行动、观察、继续决策。它回答“多步任务怎样推进”。

### Function Calling

模型用结构化字段表达某一步要调用的工具。它回答“Action 怎样传给 Host”，不负责真实执行、权限和循环。

### Structured Output

模型输出需要满足预定 Schema。它既可承载 tool call，也可承载 final answer。即使供应商支持约束解码，Host 仍要本地验证。

## 面试官可能追问

### Function Calling 已经能自动调用工具，为什么还需要 ReActExecutor？

Function Calling 通常只定义一轮中的调用意图。Executor 负责把 Observation 放回下一轮、累计状态、执行权限、预算、重复检测和最终停止。没有 Runtime，就只是一次模型响应协议，不是完整 Agent 控制循环。

### 为什么工具参数既有 Pydantic，又有 dataclass？

两者处在不同边界。Pydantic 面向不可信 JSON，负责类型转换、JSON Schema 和严格校验；dataclass 是工具内部稳定领域请求，避免工具实现绑定某个模型供应商或验证框架。代价是字段重复，后续要靠契约测试管理漂移。

### 为什么供应商已经保证 JSON Schema，还要本地校验？

供应商保证不是系统信任边界。模型版本、SDK、降级路径、测试替身和历史数据都可能产生非法对象；而且白名单和项目权限属于运行时上下文，供应商 Schema 无法替 Host 决定。

### 为什么工具白名单要检查两次？

第一层只向模型展示允许工具，减少误选；第二层在 dispatch 强制授权，防止模型幻觉、提示注入或手工构造请求绕过展示层。安全不能依赖“模型没看见”。

### 为什么单次 ToolError 不立即结束？

参数错误、找不到文件等错误本身也是 Observation，模型可能换参数或换工具恢复。但连续错误说明没有进展，所以设置确定性上限。权限错误未来也可按错误种类直接升级到 Human-in-the-loop。

### 为什么测试失败不计入连续 ToolError？

上一模块已定义 pytest 非零退出码为成功 Observation。Runtime 只看 ToolResult 是否正常完成；断言失败应该交给分析和 Reflection，不属于工具基础设施故障。

### 重复调用如何判断？

将工具名与规范化参数按键排序后序列化，计算 SHA-256 指纹。默认同一指纹第二次出现就在 dispatch 前停止，所以不会再次执行。哈希不是为了保密，而是得到固定长度、稳定可比较的标识。

### 为什么不直接缓存重复调用结果？

缓存只能减少工具成本，不能解决模型没有取得进展。最小实现选择显式停止，让外层 Reflection 或 Planner 改变策略。未来对纯读取工具可同时使用缓存，但仍需无进展检测。

### 为什么不用 LangChain Agent 或 LangGraph 预置循环？

本模块先把关键语义用几十行普通 Python 明确下来，方便单测和面试解释。后续 LangGraph 用于 Plan、Execute、Evaluate、Reflect 的外层状态机；内部 Tool Registry、模型边界和停止策略仍可复用。框架负责图编排，不替代领域约束。

### 可解释性为什么不保存完整思维链？

工程可解释性应基于可验证事实：模型收到什么、选择什么工具、参数是什么、环境返回什么、状态如何变化、为何停止。完整内部推理不稳定，也不应成为系统审计依赖，所以只保存简短决策摘要。

### 如果 Observation 越积越多怎么办？

当前最小版本全量保留，便于验证语义。生产型设计会分层保存原始 Artifact 和压缩后的上下文：保留证据引用，对旧 Observation 摘要，按 token 预算裁剪，并避免让摘要替代可追溯原文。

### 为什么同时限制 iteration 和 tool_calls？

一次 iteration 既可能是工具调用，也可能是最终回答或模型错误。迭代预算限制模型往返，工具预算单独限制外部动作和副作用；两个成本维度不同，不能合并。

### 真实模型怎么接入？

实现 RawDecisionClient，把 ModelRequest 转成供应商请求，再把响应还原成映射。StructuredDecisionModel、Registry 和 ReActExecutor 都不需要知道 OpenAI、Anthropic 或本地模型的 SDK。

## 当前代码证据

- `tools/arguments.py`：模型侧 Pydantic 参数约束与内部请求转换。
- `tools/registry.py`：工具发现、白名单、参数验证和分发。
- `react/model.py`：判别联合决策、供应商端口和脚本模型。
- `react/runtime.py`：ReAct 事件、预算、重复检测和停止条件。
- `tests/test_tool_registry_and_react.py`：14 个模块测试。

加上前两个模块，当前总计 46 个测试全部通过。

## 主动说明的局限

1. 尚未接入真实 LLM 和流式输出。
2. Observation 还没有 token 预算和分层摘要。
3. 重复检测只能识别完全相同调用。
4. 当前同步串行执行，不支持并行工具。
5. 写操作尚未引入，调用指纹还没有结合 revision 和幂等键。
6. Pydantic 与内部 dataclass 有重复定义，需要持续做契约测试。

这些局限会分别进入模型适配、LangGraph 状态机、上下文工程和受控写入模块。
