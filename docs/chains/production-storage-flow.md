# P2/P3 主链路

## Final Answer

1. Workflow 执行 `Plan -> Execute -> Evaluate -> Report`。
2. Application 只在 `status=completed` 且 Evaluator passed 时调用 `FinalAnswerSynthesizerPort`。
3. Synthesizer 从步骤摘要和评估证据提取 `path:start-end`。
4. 每条引用通过 `read_file_range` 绑定当前 `ProjectContext` 复核。
5. 无引用或复核失败的断言进入 limitations，不改变 Workflow 状态。

## MCP

1. CLI 或环境变量提供 MCP 配置路径。
2. Application 创建本地 Tool Registry 后加载配置。
3. Gateway 对 Server 执行 discover 和 tools/list。
4. 远程工具必须匹配宿主 Policy 中的 JSON Schema 结构。
5. 注册后的 MCP 工具继续受 Step allowed_tools、显式授权和结果大小限制控制。

## Storage

1. `StorageConfig` 解析 CLI、环境变量和默认值。
2. `InfrastructureFactory` 根据后端创建 RAG 和 Memory Store。
3. `CheckpointRuntimeFactory` 根据后端创建 LangGraph runtime。
4. SQLite 是默认零依赖后端。
5. PostgreSQL 后端要求先运行 Alembic 迁移，不在应用启动时临时建表。
