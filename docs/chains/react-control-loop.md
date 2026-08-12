# 最小 ReAct 控制循环

## 端到端链路

```text
用户目标 + 显式 ProjectContext
  → Host 根据任务白名单获取 Tool Registry 定义
  → 构造 ModelRequest
       ├─ user_goal
       ├─ system_instructions
       ├─ available_tools(JSON Schema + 风险元数据)
       ├─ observations
       └─ remaining_iterations / remaining_tool_calls
  → 模型返回原始结构化数据
  → StructuredDecisionModel 本地校验
       ├─ tool_call
       │    → 检查工具预算
       │    → 生成 tool_name + canonical arguments 指纹
       │    → 检查重复调用
       │    → Registry 检查存在性与任务白名单
       │    → Pydantic 校验原始参数
       │    → 转换为内部工具请求
       │    → Repository Tool 执行
       │    → ToolResult 写入 ReActEvent
       │    → 转换为 ModelObservation 进入下一轮
       └─ final_answer
            → 返回完成结果
```

## 一次成功执行的状态变化

```text
第 1 轮
  Goal: “定位 BillingService”
  Observation: []
  Decision: search_code(query="BillingService")
  Result: success + src/billing.py:1

第 2 轮
  Goal: 不变
  Observation: [search_code 的结构化结果]
  Decision: final_answer
  Result: completed
```

Agent 的“环境记忆”不是模型内部状态，而是 Host 明确保存并在下一轮重新传入的 Observation。

## 三层结构化校验

```text
模型原始 JSON
  → 决策 Schema：只能是 tool_call 或 final_answer
  → 工具参数 Schema：字段、类型、长度、数量和交叉约束
  → 领域工具校验：路径沙箱、执行授权和环境条件
```

三层不能合并：决策 Schema 管协议，参数 Schema 管单个工具形状，领域校验依赖当前项目和运行时权限。

## 停止条件

| 条件 | 在何时判断 | 为什么需要 |
| --- | --- | --- |
| 模型返回最终答案 | 决策校验后 | 正常完成 |
| 迭代预算耗尽 | 每轮上限 | 控制延迟和成本 |
| 工具调用预算耗尽 | dispatch 前 | 防止最后一轮越界执行 |
| 相同调用超过上限 | dispatch 前 | 避免无新信息循环 |
| 连续工具错误超过上限 | Observation 写入后 | 给模型一次纠错机会，同时限制盲目重试 |
| 模型调用或格式错误 | 决策边界 | 不让非法输出进入执行层 |

## Error 与 Observation

工具返回错误后，Runtime 先把错误写成 Observation。只要还未达到连续错误上限，模型下一轮可以换参数或换工具。这样“单次工具错误”与“整个 Agent 运行失败”是两个层级。

```text
missing_tool
  → ToolResult(error=not_found)
  → Observation(available_tools=...)
  → 模型改用 search_code
  → 继续执行
```

## Trace 保存什么

每个 ReActEvent 保存：迭代号、工具名、原始参数、简短决策摘要、调用指纹和完整 ToolResult。最终结果保存状态、答案、迭代数、工具次数和停止原因。

它足以回答“为什么调用、调用了什么、环境返回什么、为何停止”，但不保存完整隐藏思维链。
