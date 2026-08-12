# 失败案例：把 Checkpoint 当成外部副作用 exactly-once

## 现象

节点调用远程 API 或写入文件成功后，进程在节点状态写入 checkpoint 前崩溃。恢复时 LangGraph 从上一个完整状态边界重放节点，外部动作执行第二次。

## 根因

Checkpoint 数据库和外部系统不是同一个事务。状态持久化只能证明 Graph 看到了什么，无法回滚或自动去重已经发生的外部副作用。

## 当前处理

Graph 为每次步骤尝试生成稳定 `execution_key`。如果是同一未提交尝试的重放，key 相同；如果是 Reflection 决定的新 Retry，attempt 增加，key 不同。

当前项目仍以只读工具为主，所以 key 只进入 StepExecutor 契约和 Trace。未来写工具必须：

1. 在执行前查询 key 是否已有成功记录；
2. 已成功则返回保存结果，不再次执行；
3. 未执行则记录 pending、执行副作用、保存结果；
4. 对目标系统使用原生幂等键或本地 outbox。

## 面试结论

Checkpoint 提供 at-least-once 恢复基础，不天然提供跨系统 exactly-once。真正需要保证的是副作用幂等，而不是宣称永不重放。
