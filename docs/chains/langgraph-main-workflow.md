# LangGraph 主闭环逻辑链

## 节点与条件边

```text
START
  → plan
      ├─ 规划失败 → report → END
      └─ 成功 → execute_step
                   ├─ 还有步骤 → execute_step
                   ├─ 步骤失败 → evaluate
                   └─ 全部完成 → evaluate
                                      ├─ 通过 → report → END
                                      ├─ 反思预算耗尽 → report → END
                                      └─ reflect
                                           ├─ 局部错误 → retry execute_step
                                           ├─ 计划错误且有预算 → replan → execute_step
                                           └─ 重规划预算耗尽/异常 → report → END
```

## State 的关键字段

| 字段 | 含义 | 谁更新 |
| --- | --- | --- |
| `project_id/repo_root/repo_revision` | 当前显式目标仓库与版本 | Run 初始化 |
| `plan` | 当前生效计划 | plan/replan/reflect |
| `current_step_index` | 下一条要执行的步骤 | execute/reflect/replan |
| `step_results` | 所有步骤尝试的追加式结果 | execute |
| `evaluation` | 当前评估 | evaluate |
| `reflection` | 最近一次失败分析 | reflect |
| `reflection_count/replan_count` | 业务循环预算 | reflect/replan |
| `status/stop_reason` | 最终控制状态 | 异常节点/report |
| `trace` | 节点级可审计事件 | 所有节点 |

`plan_history`、`evaluation_history`、`reflection_history` 使用 reducer 追加。当前值用于路由，历史值用于复盘，二者职责不同。

## 成功路径

```text
plan(created: 2 steps)
  → execute_step(locate: completed)
  → execute_step(explain: completed)
  → evaluate(passed)
  → report(completed)
```

## 局部重试路径

```text
execute_step(search: failed)
  → evaluate(rejected: 搜索证据不足)
  → reflect(retry: 缩小文件范围)
  → execute_step(search: completed, attempts=2)
  → evaluate(passed)
  → report(completed)
```

Retry 不生成新计划，只重置当前失败步骤。旧 StepExecution 不删除，因此可以比较第一次和第二次工具轨迹。

## 重规划路径

```text
execute_step(locate: completed)
  → evaluate(rejected: 缺少独立验证)
  → reflect(replan: 原计划不完整)
  → replan(保留 locate，追加 verify)
  → execute_step(verify: completed)
  → evaluate(passed)
  → report(completed)
```

Replan 不是从零覆盖一切：已完成前缀保留，失败步骤和未完成步骤被替代，旧计划保存在 plan_history。

## 两层循环预算

```text
内层 ReAct：iteration/tool/error/duplicate budgets
外层 Graph：reflection/replan budgets
全图兜底：LangGraph recursion_limit
```

内层预算控制一次步骤探索；外层预算控制任务策略修正；recursion_limit 防御代码路由缺陷。它们解决的故障层级不同。

## 为什么 Evaluator 独立

Executor 的职责是完成当前步骤并报告事实。Evaluator 的职责是根据任务模式和外部证据判断整体目标是否满足。如果让 Executor 自己宣布成功，就容易把“模型给出了答案”误当成“测试、编译或证据已经通过”。
