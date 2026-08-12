# SQLite Checkpoint 恢复链路

## Start 链路

```text
显式 ProjectContext + logical thread_id + user goal
  → 校验逻辑 thread id
  → project checkpoint namespace + logical id
  → physical thread_id
  → 查询 SQLite：线程是否已存在
       ├─ 已存在 → 拒绝 start，要求 resume 或更换 id
       └─ 不存在 → 构造初始 Graph State
  → graph.invoke(initial_state, configurable.thread_id)
  → 每个 super-step 保存 checkpoint
  → 完成或停在 interrupt 边界
```

## 跨实例 Resume 链路

```text
进程一关闭 SQLite 连接
  → 进程二创建新的 SqliteSaver 和 RepoAgentWorkflow
  → 使用相同 ProjectContext + logical thread_id
  → 生成相同 physical thread_id
  → get_state 读取最新 snapshot
  → 校验 project_id
  → 校验 repo_root
  → 校验 repo_revision
       ├─ 不一致 → 拒绝恢复
       └─ 一致 → graph.invoke(None, config)
  → 从 snapshot.next 指向的节点继续
```

测试中的实际路径：

```text
plan
  → execute_step
  → interrupt_before(evaluate)
  → SQLite 持久化，关闭 Runtime
  → 新 Runtime resume
  → evaluate
  → report
```

第二个 Runtime 的 Planner 和 StepExecutor 都没有脚本响应。如果恢复错误地从头开始，测试会立刻失败。这证明已经完成的节点没有重复运行。

## 标识关系

```text
project_id
  └─ physical thread_id
       ├─ logical thread_id
       └─ checkpoint history
            ├─ checkpoint A：input
            ├─ checkpoint B：plan 完成
            ├─ checkpoint C：execute 完成，next=evaluate
            └─ checkpoint D：report 完成，next=()

run_id 保存在所有状态与步骤 execution key 中
```

## 恢复与副作用

```text
节点开始
  → 计算 execution_key(run_id, step_id, attempt)
  → 执行外部动作
  → 返回节点更新
  → checkpoint 提交
```

如果进程在外部动作完成后、checkpoint 提交前崩溃，恢复时节点可能重放。Checkpoint 保证状态恢复，不自动保证外部动作 exactly-once。写操作必须用相同 execution key 查询是否已经执行。

## 为什么 revision 只阻止恢复，不阻止历史查询

历史快照描述的是旧版本上真实发生过的运行，代码更新后仍有审计价值。但继续执行会把旧计划和 Observation 应用到新代码，必须显式 rebase 或重新规划，不能静默发生。
