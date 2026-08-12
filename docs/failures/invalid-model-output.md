# 失败案例：把模型结构化输出当成可信对象

## 现象

模型可能返回缺字段、字段类型错误、未知决策类型、额外字段，甚至供应商调用本身失败。如果代码直接读取 `tool_name` 和 `arguments`，错误会在执行深处以 KeyError、TypeError 或意外参数形式爆炸。

## 根因

Function Calling 降低了自由文本解析成本，但不等于输出永远合法。模型、供应商 SDK、提示词版本、测试替身和网络层都位于系统信任边界之外。

## 错误做法

```text
raw = model(...)
tools[raw["tool_name"]](**raw["arguments"])
```

这段逻辑没有决策类型校验、工具存在性检查、参数 Schema、白名单或统一异常语义。

## 当前处理

1. StructuredDecisionModel 先把原始映射校验为判别联合类型。
2. Tool Registry 再校验工具是否存在和是否在任务白名单。
3. 对应 Pydantic 参数模型拒绝缺失、额外或越界参数。
4. 领域工具继续执行路径和权限校验。
5. 模型决策错误返回 `model_error`；工具参数错误返回结构化 ToolResult，可成为 Observation。

## 面试结论

“结构化输出”是协议能力，不是信任证明。Agent 的可靠性来自 Host 端逐层校验和稳定错误分类。
