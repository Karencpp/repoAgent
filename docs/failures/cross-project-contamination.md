# Failure card：跨项目状态污染

## 故障场景

用户先分析 `order-service`，Agent 记住“金额使用 Decimal”；随后切换到 `analytics-service`。如果 Memory、RAG 或工具缓存只按 session 或全局 key 存储，第二个项目可能错误继承前一个项目的结论。

## 表面症状

- Agent 引用了目标仓库中不存在的文件或规范；
- 检索结果路径来自另一个项目；
- 新项目第一次运行却出现“根据我们上次的结论”；
- Checkpoint 恢复到了错误计划；
- 工具路径仍指向旧 repo root。

## 根因

把“当前项目”保存在可变全局变量，或没有把 `project_id` 纳入 Memory、RAG、Checkpoint、Evidence 和缓存 key。

## 诊断顺序

1. 查看 Run Trace 中的 `project_id、repo_root、revision`。
2. 查看 Memory/RAG 查询是否包含 project namespace。
3. 查看 Evidence 的路径能否通过当前 `ProjectContext.resolve_repo_path`。
4. 查看 Checkpoint thread 是否绑定当前 project。
5. 查看缓存 key 是否只使用 query、path 或 session_id。

## 修复策略

- 每个 Run 使用不可变 `ProjectContext`；
- 所有持久化和缓存 key 包含稳定 `project_id`；
- 代码 Evidence 同时包含 revision 或内容指纹；
- 切换项目创建新 thread，不复用未完成 Graph State；
- 工具只能通过当前 Context 解析路径。

## 本模块如何预防

- Resolver 强制显式选择目标；
- 不使用 `cwd` 和全局 current project；
- Context 派生三个独立 namespace；
- 两项目 namespace 隔离已有自动测试；
- 路径沙箱已有逃逸测试。

## 尚未覆盖

Memory、RAG 和 Checkpoint 目前尚未实现。后续模块必须把这里的 namespace 契约接入真实存储，并增加端到端串库测试。

