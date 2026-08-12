# Failure card：把测试失败误判成工具失败

## 故障场景

pytest 正常执行并返回 `exit_code=1`，输出显示一个断言失败。工具包装层却把任何非零退出码都转换成 `execution_error`。

## 错误后果

- Agent 认为测试基础设施不稳定，重复运行相同命令；
- Reflection 看不到断言、堆栈和失败用例；
- 重试预算被浪费；
- Trace 无法区分“代码不正确”和“工具没运行起来”；
- 评测将有效失败证据统计成工具可用性问题。

## 正确分层

```text
工具层成功：进程启动并返回完整 Observation
业务层失败：测试断言没有通过
```

只有超时、进程无法启动、权限拒绝和参数无效等才属于工具错误。

## 本模块如何预防

- `ToolResult.status` 表示工具调用是否正常完成；
- `ProcessResult.exit_code` 表示测试业务结果；
- metadata 单独提供 `tests_passed`；
- 测试验证 exit code 1 时 `result.ok` 仍为 true；
- 超时使用 `TIMEOUT` 并保留部分 ProcessResult。

## 延伸到 Agent Graph

```text
tests_passed=false
  → Evaluator 判定候选补丁未达标
  → Reflection 分析失败证据

ToolErrorKind.TIMEOUT
  → 工具错误处理分支
  → 缩小测试范围、增加合理超时或有限重试
```

两个分支不能共用同一种 Retry 策略。

