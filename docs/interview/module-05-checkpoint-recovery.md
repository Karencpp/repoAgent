# 模块 05 面试讲解：SQLite Checkpoint 与恢复

## 30 秒回答

我给 LangGraph 主图接入了 SQLite SqliteSaver。用户使用逻辑 thread id，Host 将 project checkpoint namespace 拼到物理 thread id 上，因此不同代码库可以安全使用相同任务名。Start 会拒绝覆盖已有线程，Resume 会校验 project_id、repo_root 和 repo_revision，再从 `snapshot.next` 继续。测试真正关闭第一个 SQLite 连接，用全新 Workflow 实例恢复，并证明已完成的 Execute 节点没有重跑。Checkpoint 只恢复状态，不保证外部副作用 exactly-once，所以步骤还有稳定 execution key。

## 2 分钟回答

LangGraph 会在 super-step 边界保存 StateSnapshot，一个 thread 下有多个 checkpoint。thread_id 是状态谱系，run_id 是维护任务的审计身份，checkpoint_id 是某个时间点的快照。项目还必须先隔离，所以物理 thread key 由 project namespace 和用户逻辑 thread id 共同组成。

我把 start 和 resume 分开：start 发现线程已存在就拒绝，避免把新初始状态经过 reducer 合并进旧任务；resume 先读取快照，并验证项目身份、路径和代码 revision。代码已经变化时，旧 Evidence 可能失效，所以允许查看历史，但拒绝继续执行。

SQLite Runtime 管理连接和严格 serializer 白名单，不使用 pickle fallback。跨实例测试先让图停在 evaluate 前，关闭连接，再创建没有 Planner 和 Executor 响应的新 Runtime。如果恢复从头执行就会失败；实际只运行 evaluate 和 report。

最后，checkpoint 与幂等不是一回事。节点可能在外部动作成功后、checkpoint 提交前崩溃，所以恢复会重放节点。我用 run_id、step_id 和 attempt 生成 execution key。同一次重放 key 相同，主动 Retry key 不同。未来写工具需要真正消费这个 key 才能去重。

## 五个标识必须分清

### project_id

代码库长期身份。用于隔离 Checkpoint、Memory 和 RAG。

### thread_id

一条可恢复的状态谱系。暂停和恢复都使用同一个逻辑 thread id。

### run_id

一次维护任务的审计身份，进入 Trace 和 execution key。当前一个 thread 对应一个维护 run。

### checkpoint_id

某个 super-step 后的具体 StateSnapshot。一个 thread 有多个 checkpoint。

### checkpoint_ns

LangGraph 用来区分根图和子图的 namespace。根图为空；它不是项目租户字段。

## 面试官可能追问

### Checkpoint 和 Memory 有什么区别？

Checkpoint 保存当前执行状态，用于同一个 thread 的暂停和恢复；Memory 保存经过验证、跨任务仍有价值的项目事实。Checkpoint 可以包含失败中间态，不应该自动变成长期知识。

### 为什么 thread_id 不能直接等于 project_id？

一个项目会有很多独立任务。如果都写进同一 thread，新的输入会与旧计划、旧 reducer 历史合并，无法区分任务边界。

### 为什么要区分逻辑和物理 thread id？

用户希望使用 `task-1` 这种可读名称，但两个项目可能重名。Host 将项目 namespace 加到数据库 key 上，既保留用户体验，又在持久化层强制隔离。

### 为什么不用 checkpoint_ns 隔离项目？

框架将 checkpoint_ns 定义为根图和子图的层级空间。挪作项目隔离会与子图 namespace 语义冲突，因此项目身份进入 physical thread id，根图 namespace 仍为空。

### 为什么不能对已有线程再次 start？

带 reducer 的 State 会把新输入追加到旧值，可能把两个任务的 step results 和 trace 混在一起。已有线程必须 resume；新任务必须创建新 thread。

### 为什么 revision 改了就不能恢复？

计划、搜索结果、路径和行号都属于旧版本。静默继续等于把旧推理应用到新环境。正确做法是显式重新规划或未来实现 checkpoint rebase。

### 已完成线程再次 resume 会怎样？

`snapshot.next` 为空，Runtime 直接返回保存结果，不再调用 Planner、Executor 或 Evaluator。测试对此有明确断言。

### SQLite 适合生产吗？

适合本地、单进程或低并发。本项目不部署，所以它是成本最低的选择。多实例并发、连接池、高可用和集中运维出现时，应切 PostgresSaver。

### Checkpoint 是否保证工具只执行一次？

不保证。外部副作用和 checkpoint 不在同一事务里。恢复语义通常需要按 at-least-once 设计，依赖幂等键、去重表或目标系统原生幂等接口。

### execution key 为什么包含 attempt？

崩溃重放同一次尝试时 attempt 不变，需要复用相同 key；Reflection 明确决定重试时是一次新的业务尝试，应得到新 key并保留两次结果。

### 为什么 serializer 要白名单？

Checkpoint 是持久化输入，数据库被替换时反序列化也属于信任边界。只允许项目明确使用的状态类型，比允许导入任意模块或启用 pickle fallback 更稳妥。但它不等于数据加密。

### 这次开发遇到了什么真实故障？

内存测试中 tuple reducer 正常，但 msgpack 恢复后裸 tuple 变成 list，下一次追加发生类型错误。修复方式是内部 channel 统一用 list，对外边界再转 tuple，并增加真正的跨连接恢复测试。

## 当前代码证据

- `workflow/checkpoints.py`：SQLite 生命周期、线程隔离、恢复校验、历史和删除。
- `workflow/graph.py`：checkpointer 编译、interrupt 状态、resume 和 execution key。
- `workflow/models.py`：thread id、interrupted 结果和幂等键记录。
- `tests/test_sqlite_checkpoints.py`：10 个持久化与恢复测试。

全项目当前 71 个测试全部通过。

## 主动说明的局限

1. 只支持恢复最新 checkpoint，尚无 time travel/fork。
2. 没有 checkpoint 保留和压缩策略。
3. SQLite 不面向多实例生产并发。
4. 当前 execution key 尚未接入写工具的真实去重表。
5. 没有加密 SQLite 文件。
6. 尚未实现人工审批节点，只用静态 interrupt 验证恢复语义。
