# ADR 013：Final Answer、MCP 装配与 PostgreSQL 持久化

日期：2026-08-08

## 状态

已接受。

## 背景

P2/P3 要求把已有半集成能力接入主链路，并把 SQLite 默认开发后端抽象为可替换的生产后端。核心风险是：最终答案可能越过 Evaluator 编造结论，MCP Server 目录可能被当成授权，PostgreSQL 类型可能泄漏到 Graph 和领域层。

## 决策

- 新增 `FinalAnswerSynthesizerPort`，默认实现只消费通过 Evaluator 的步骤结果和 Evidence。
- 引用统一按 `path:start-end` 提取，并通过 `read_file_range` 在当前 revision 重新复核。
- `RepoAgentApplicationConfig` 接收 MCP 配置路径；Application 启动时发现、审核并注册 MCP 工具。
- 新增 `RAGIndexPort`、`MemoryStorePort`、`CheckpointRuntimeFactory`、`StorageConfig` 和 `InfrastructureFactory`。
- SQLite 仍是默认后端；PostgreSQL/pgvector 通过可选依赖、DSN、Alembic 迁移和 Docker Compose 显式启用。

## 后果

- Final Answer 失败不会推翻客观 Workflow 状态，只会在报告中暴露生成失败。
- MCP 远程描述和 annotations 不进入宿主授权；只有 Policy 白名单能注册为工具。
- Graph 和业务模型不依赖 pgvector 类型。
- 默认离线测试不需要 Docker；真实 PostgreSQL 集成必须由环境显式开启。
