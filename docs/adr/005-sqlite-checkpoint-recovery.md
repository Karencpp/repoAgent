# ADR-005：按项目隔离的 SQLite Checkpoint 与恢复语义

- 状态：Accepted
- 日期：2026-07-31
- 模块：SQLite checkpoint and recovery

## 背景

LangGraph 主图已经有显式状态，但进程退出后状态仍会消失。代码维护任务可能因为人工审批、模型故障或运行环境重启而暂停，需要从最近的状态边界继续，同时不能重复已经完成的节点或把其他项目的状态加载进来。

Checkpoint 还引出三个容易混淆的问题：thread 与 run 如何区分、目标仓库变化后能否恢复，以及恢复是否自动保证外部副作用 exactly-once。

## 决策

### 1. 使用 SQLite SqliteSaver

本项目是本地、单用户、面试导向，不需要部署数据库服务。SQLite 能提供持久化 checkpoint 历史、跨进程恢复和直接检查能力，成本低于 Postgres。

Runtime 使用同步 SqliteSaver，连接生命周期由 `SQLiteWorkflowRuntime` 上下文管理器负责。打开时创建表、编译绑定 checkpointer 的图，退出时关闭连接。

### 2. 区分四个标识

| 标识 | 生命周期 | 作用 |
| --- | --- | --- |
| `project_id` | 项目长期稳定 | 隔离不同目标代码库 |
| `thread_id` | 一条任务状态谱系 | 暂停、恢复和查询 checkpoint |
| `run_id` | 一次维护任务的审计身份 | 关联 Trace、步骤和幂等键 |
| `checkpoint_id` | 一个 super-step 快照 | 历史查询、调试和未来 time travel |

恢复同一任务时 thread_id 和 run_id 都保持不变。重新开始另一个目标必须使用新 thread_id，不能向旧线程注入一份新的初始 State。

### 3. 逻辑线程与物理线程分离

用户传入可读的逻辑 thread id，例如 `fix-order-total`。Host 使用：

```text
physical_thread_id = project.checkpoint_namespace + ":" + logical_thread_id
```

作为 SqliteSaver 的真实 key。因此两个项目都使用 `task-1` 时不会串状态。

LangGraph 的 `checkpoint_ns` 保持为空字符串，因为它表示根图；子图会使用自己的 namespace。项目隔离不滥用这个框架字段。

### 4. Start 与 Resume 明确分离

- `start`：如果物理线程已经存在则拒绝，防止 reducer 把新初始状态合并进旧线程。
- `resume`：只接受已有线程，先校验 project_id、repo_root 和 repo_revision，再用 `invoke(None, config)` 从保存状态继续。
- 已经完成的线程再次 resume：直接返回最终保存状态，不重新执行节点。

### 5. 仓库 revision 变化时拒绝继续执行

Checkpoint 中的 Observation 和计划针对旧代码版本。恢复前如果当前 ProjectContext revision 不一致，Runtime 抛出明确错误，而不是在新代码上继续旧计划。

历史仍可读取，因为审计旧运行不要求当前代码版本相同；只有继续执行要求版本一致。

### 6. 使用严格序列化白名单

默认序列化器不启用 pickle fallback，只允许恢复 RepoAgent 明确列出的 Pydantic 状态类型。这样即使 checkpoint 数据库被替换，也不会任意导入所有 msgpack 类型。

这不是数据库加密。数据库内容仍应放在 RepoAgent 自己的状态目录，并依赖文件权限保护。

### 7. 为步骤尝试生成稳定 execution key

每次步骤尝试使用：

```text
SHA256(run_id + step_id + attempt)
```

同一节点在 checkpoint 提交前崩溃并被重放时，run、step 和 attempt 不变，因此 key 不变；Reflection 后主动 Retry 会增加 attempt，得到新 key。

当前只把 key 传到 StepExecutor 并写入 StepExecution。未来写文件、提交补丁或调用远程服务时，副作用层必须真正消费这个 key，才能实现去重。

## Checkpoint 与 Memory 的区别

Checkpoint 保存原始运行状态，是 thread 级恢复机制；Memory 保存经过筛选、跨任务仍有价值的项目事实。

```text
Checkpoint：当前执行到 step-2，下一节点 evaluate
Memory：该项目金额统一使用 Decimal
```

不能因为 checkpoint 数据可持久化，就把全部运行历史当作长期知识自动召回。

## 没有选择的方案

### InMemorySaver

适合单测，但进程退出后丢失，无法证明真正的跨实例恢复。

### 一开始使用 Postgres

适合多进程、高并发和生产服务，但本项目没有部署需求。引入服务、连接池和迁移不会增加当前面试主链路的理解价值。

### 用 project_id 直接充当 thread_id

一个项目会有多次独立维护任务。如果只用 project_id，新的输入会继续累积到同一线程，计划、Trace 和 reducer 历史相互污染。

### revision 变化后自动继续

旧 Evidence 的路径和行号可能已经失效。自动恢复看似方便，但会把“恢复运行”变成未经验证的隐式 rebase，因此拒绝。

## 代价与局限

- SQLite 适合本地串行或低并发，不是生产多实例 checkpoint 后端。
- 当前没有 checkpoint 压缩和保留策略，长线程会增长。
- 当前只支持从最新 checkpoint 恢复，尚未提供 checkpoint_id time travel/fork API。
- 静态 interrupt 已用于验证恢复，真正的 Human-in-the-loop 审批节点尚未实现。
- execution key 目前只进入执行契约，现有只读工具不需要去重；未来写工具必须实现幂等存储。
- SQLite 文件没有应用层加密。

## 验证证据

测试覆盖：

- 完成状态、最新快照和历史持久化；
- 关闭第一个 Runtime 后，用全新实例恢复；
- 已完成的 Execute 节点在恢复时不重复运行；
- 已完成线程再次 resume 不调用任何节点；
- 相同逻辑 thread id 在两个项目中互相隔离；
- 已有线程不能再次 start；
- 仓库 revision 变化后历史可读，但 resume 被拒绝；
- history limit 与项目级 delete；
- 非法逻辑 thread id 拒绝；
- Runtime 关闭后的访问拒绝；
- Reflection Retry 生成不同 execution key；
- 严格序列化允许 Pydantic Graph State 跨连接恢复。

全项目当前 71 个测试通过。

## 未来切换条件

- 多进程并发和生产部署：切换 PostgresSaver。
- 大量历史增长：增加 checkpoint 清理、归档和保留策略。
- 人工批准写操作：在 approval node 使用 interrupt/resume。
- 时间旅行调试：允许选择 checkpoint_id 并 fork 新 thread。
- 写入外部系统：建立 execution key 去重表或目标系统幂等接口。
