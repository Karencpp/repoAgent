# RepoAgent

RepoAgent 是一个面向 Python 代码库的可解释代码维护 Agent。它把大模型放在受控的软件工程闭环中：显式选择目标仓库，使用 LangGraph 编排任务，通过受限 ReAct 调用工具，并用代码引用、编译和测试结果验证输出。

项目当前提供本地 CLI，支持两条真实链路：

- **Explain**：只读分析代码库，输出经过 Evaluator 验收和源码引用复核的答案。
- **Fix**：在独立候选副本中生成补丁、运行客观验证，等待人工审批后才回写真实仓库。

> 当前定位是可运行、可评测的本地工程实现，不是生产级多租户服务。SQLite 是默认开发后端；PostgreSQL/pgvector 是可选后端。

## 核心能力

- **多代码库隔离**：每次运行必须显式传入 `--repo` 或 `--project`；`ProjectContext` 绑定稳定项目身份、仓库根目录和当前 revision。
- **双工作流**：只读链路使用 Plan / Execute / Evaluate / Reflect / Replan；维护链路独立管理分析、补丁、验证、反思、重试、审批和回写。
- **受控 ReAct 与 Tool Registry**：模型只提交结构化决策；宿主执行工具白名单、JSON Schema、参数、预算和权限校验。
- **代码库 RAG**：Python AST、Markdown 标题和通用文本结构化分块，SHA-256 增量索引，BM25/Dense/RRF 混合检索并携带源码范围。
- **Memory 与 Context Engineering**：区分 Working State、Checkpoint、长期 Memory 和代码 RAG；按项目、revision、可信状态和 Token 预算组织上下文。
- **Checkpoint 恢复**：SQLite 或 PostgreSQL 保存 LangGraph 状态；恢复前校验项目身份和代码 revision，拒绝在代码已经变化时静默续跑。
- **安全候选修改**：补丁先写入独立工作副本，再依次验证变更范围、Python 编译、目标测试和回归测试。
- **持久化人工审批**：维护工作流在 `await_approval` 节点暂停，可通过 `resume-fix --approve/--reject` 恢复。
- **Final Answer 引用复核**：最终答案只消费已通过 Evaluator 的结果，并重新读取当前源码复核 `path:start-end` 引用。
- **Agent Skill**：可信目录中的能力包支持渐进加载、确定性路由、参考资料、受控脚本、双向 Schema、版本和完整包哈希。
- **MCP Gateway**：支持本地 Registry 和现代 HTTP Server；远程工具只有匹配宿主 Policy 后才能进入 Tool Registry。
- **双存储后端**：默认使用 SQLite；可选 PostgreSQL FTS、pgvector HNSW、PostgreSQL Memory 和 LangGraph PostgresSaver。
- **离线评测**：内置 Retrieval、Explain、Patch 数据集和可重复 Runner；另有 Django RAG、GLM Embedding/Rerank、Skill 和编排 A/B benchmark。

## 工作流概览

### 只读解释

```text
ProjectContext
  -> RAG / verified Memory 预检索
  -> Plan
  -> Execute（每个步骤内部运行受控 ReAct）
  -> Evaluate
  -> Reflect / Replan（失败时，受预算限制）
  -> Final Answer（重新复核源码引用）
```

### 代码维护

```text
Analyze Repository
  -> Select Targets
  -> Propose Patch
  -> Evaluate Patch
       |-- failed -> Reflect -> Repatch / Reselect / Stop
       `-- passed -> Persist Proposal -> Await Approval
                                         |-- approve -> Promote Patch
                                         `-- reject  -> Report Rejected
```

真实仓库在审批前不会被修改。工作副本隔离不等于操作系统沙箱；运行不可信仓库仍需要容器或受限账户。

## 环境要求与安装

- Python 3.12+
- GLM 或 DeepSeek API Key，用于真实 Planner、ReAct 和 Reflector
- 可选：Docker Compose，用于 PostgreSQL/pgvector 后端与 benchmark

建议使用虚拟环境安装：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e .
```

配置推理供应商。默认使用 GLM：

```powershell
$env:ZHIPUAI_API_KEY="你的 GLM 密钥"
```

也可以切换到 DeepSeek：

```powershell
$env:DEEPSEEK_API_KEY="你的 DeepSeek 密钥"
repo-agent explain --llm-provider deepseek --repo "D:\code\target-project" "解释核心调用链"
```

推理供应商只影响模型调用，不改变工作流、工具权限、RAG、Memory 和候选验证协议。

## 快速开始

### 1. 解释代码库

```powershell
repo-agent --state-dir ".\output\my-state" explain `
  --repo "D:\code\target-project" `
  "这个项目的入口和核心调用链是什么？"
```

RepoAgent 不会使用当前工作目录兜底。RAG、Memory、Checkpoint 和项目注册表都写入独立的 `state-dir`，不会混入目标仓库。

### 2. 连续问答

```powershell
repo-agent --state-dir ".\output\my-state" chat `
  --repo "D:\code\target-project"
```

输入 `退出`、`exit` 或 `quit` 结束。每个问题都会形成独立的可恢复任务。

### 3. 注册常用项目

```powershell
repo-agent --state-dir ".\output\my-state" project add `
  --repo "D:\code\target-project" `
  --name target-project

repo-agent --state-dir ".\output\my-state" project list
repo-agent --state-dir ".\output\my-state" explain `
  --project target-project `
  "请求进入系统后经过哪些模块？"
```

### 4. 恢复只读任务

只有目标项目、路径和 revision 仍然匹配时才能恢复：

```powershell
repo-agent --state-dir ".\output\my-state" resume `
  --project target-project `
  --thread-id run-20260813T120000Z-abcd1234
```

## 生成、验证并审批补丁

`--allow-code-execution` 明确授权 RepoAgent 在候选副本中运行目标仓库测试。缺少该参数时，验证会停在权限边界。

```powershell
repo-agent --state-dir ".\output\my-state" fix `
  --repo "D:\code\target-project" `
  --thread-id fix-add-bug `
  --allow-code-execution `
  "修复 add 函数的计算错误"
```

命令会输出 unified diff、客观验证报告、`proposal_id` 和线程标识。验证通过后，工作流停在持久化审批节点。

批准并回写：

```powershell
repo-agent --state-dir ".\output\my-state" resume-fix `
  --repo "D:\code\target-project" `
  --thread-id fix-add-bug `
  --approve
```

拒绝候选：

```powershell
repo-agent --state-dir ".\output\my-state" resume-fix `
  --repo "D:\code\target-project" `
  --thread-id fix-add-bug `
  --reject
```

旧 proposal 文件仍可通过兼容入口批准：

```powershell
repo-agent --state-dir ".\output\my-state" apply `
  --proposal-id proposal-xxxxxxxxxxxxxxxxxxxx `
  --approve
```

维护链路遵守以下不变量：

- 模型只生成结构化修改草稿，旧文件 SHA-256 由宿主从真实基线读取。
- 所有文件先完成路径、类型、编码和哈希校验，再写入候选副本。
- Evaluator 依次检查变更范围、Python 编译、目标测试和回归测试；任何 skipped 都不算通过。
- pytest 优先使用目标仓库的 `.venv`、`venv` 或 `env`，找不到时才显式降级到宿主解释器。
- 未通过验证、未获得审批或源仓库 revision/文件哈希发生变化时，回写会被拒绝。
- 回写使用同目录原子替换；多文件写入中途失败时会尝试恢复已经修改的文件。

## RAG、Embedding 与 Reranker

默认使用 256 维 `FeatureHashEmbeddingClient`。它无需联网，适合离线协议测试和稳定回归，但不是真正的语义 Embedding。

允许源码发送到外部服务后，可以显式启用 GLM Embedding：

```powershell
$env:ALLOW_EXTERNAL_CODE_EMBEDDING="true"
repo-agent explain --repo "D:\code\target-project" `
  --use-glm-embedding `
  "解释核心领域模型"
```

GLM Reranker 当前已经实现供应商无关接口、HTTP 适配器和 benchmark，但尚未作为普通 `explain` 命令的默认检索阶段。外部重排必须单独设置 `ALLOW_EXTERNAL_CODE_RERANKING=true`。

## Agent Skill

仓库内置两个可信 Skill 示例：

- `diagnose-pytest-failure`：根据 pytest 观察确定性分类测试失败。
- `safe-python-refactor`：比较 Python 公共 API 和签名变化。

默认从应用配置的可信 Skill 根目录发现能力，也可以显式指定：

```powershell
repo-agent explain --repo "D:\code\target-project" `
  --skills-root ".\skills" `
  "分析测试失败原因"
```

目标代码库中的 `SKILL.md` 或脚本不会自动成为可信指令。Skill 只能收窄现有工具权限，不能授予新权限；脚本仍通过 Tool Registry、Schema、超时和输出上限执行。

## MCP Gateway

通过配置文件装配 MCP Server：

```powershell
repo-agent explain --repo "D:\code\target-project" `
  --mcp-config ".\configs\mcp.local-registry.example.json" `
  "列出入口文件"
```

也可以设置：

```powershell
$env:REPO_AGENT_MCP_CONFIG=".\configs\mcp.local-registry.example.json"
```

配置支持本地 `registry` 和远程 `http` transport。Server 的工具声明、description 和 annotations 都不等于授权；只有通过本地 Policy 审核、Schema 对齐和风险约束的工具才会注册。

## 长期 Memory

只读和维护任务可以自动形成受 Curator 治理的情景记忆和语义候选。跨多次任务的慢路径归纳需要显式触发：

```powershell
repo-agent --state-dir ".\output\my-state" memory consolidate `
  --repo "D:\code\target-project" `
  --topic "测试失败模式"
```

模型不能直接写入 verified Memory。长期事实需要经过 Evidence、scope、revision、TTL、冲突和审核策略。

## PostgreSQL / pgvector 可选后端

SQLite 是零部署默认后端。启用 PostgreSQL 后，RAG 使用 PostgreSQL FTS、trigram 和 pgvector HNSW，Memory 与 LangGraph Checkpoint 也切换到 PostgreSQL 实现。

```powershell
docker compose -f docker-compose.postgres.yml up -d

$env:REPO_AGENT_STORAGE_BACKEND="postgres"
$env:REPO_AGENT_POSTGRES_DSN="postgresql://repo_agent:repo_agent_dev_password@localhost:54329/repo_agent"

python -m pip install -e ".[postgres]"
alembic upgrade head

repo-agent explain --storage-backend postgres `
  --repo "D:\code\target-project" `
  "解释入口"
```

应用不会在启动时临时建表；缺少可选依赖、DSN 或迁移时会尽早失败。

SQLite 状态可以先 dry-run，再事务化迁移 RAG 和 Memory 数据：

```powershell
repo-agent migrate-state `
  --sqlite-state-dir ".\output\my-state" `
  --postgres-dsn $env:REPO_AGENT_POSTGRES_DSN `
  --dry-run

repo-agent migrate-state `
  --sqlite-state-dir ".\output\my-state" `
  --postgres-dsn $env:REPO_AGENT_POSTGRES_DSN `
  --execute
```

Checkpoint 不做猜测转换。已有 SQLite 线程应继续在 SQLite 中完成，或在 PostgreSQL 后端创建新任务。

## 测试与离线评测

运行全部测试：

```powershell
python -m unittest discover -s tests -v
```

2026-08-13 在仓库虚拟环境中的结果：**244 个测试通过，3 个按外部条件或 Windows 能力跳过**。

运行内置离线评测：

```powershell
repo-agent eval retrieval --dataset evals/retrieval/python-small.jsonl
repo-agent eval explain --dataset evals/explain/python-small.jsonl
repo-agent eval patch --dataset evals/patch/python-small.jsonl
```

同日实测结果：

| Suite | Case | 结果 |
| --- | ---: | --- |
| Retrieval | 5 | 全部通过，Mean Recall@K = 1.0，最低 MRR = 0.3333 |
| Explain | 2 | 全部通过 |
| Patch | 2 | 全部通过，2 次 Patch 尝试 |

这些数据集是小型、确定性、本地夹具，不调用真实 LLM，也不代表生产任务总体成功率。数据格式和口径见 [`evals/README.md`](evals/README.md)。

仓库还保留了 2026-08-08 至 2026-08-09 的大型实测结果：

- Django Core 120 条查询、GLM `embedding-3` 512 维：Hybrid Recall@10 85.83%，Hit@10 94.17%。
- Hybrid Top 40 + GLM Rerank：Rerank Top 20 的 MRR@10 为 80.99%，Hit@10 为 97.50%。
- Django 17,380 Chunk 存储基准：pgvector HNSW Dense P95 1.28 ms；SQLite Python 精确扫描 P95 2524.41 ms。该对比只代表本项目两种实现。

原始结果和限制说明位于 [`output/benchmarks`](output/benchmarks)。真实 API benchmark 会产生费用，并要求显式密钥和源码外发授权。

## 当前边界

- 主要面向 Python；尚未实现多语言解析和跨文件调用图。
- 候选工作副本、路径沙箱和受控子进程都不是 OS 级执行沙箱。
- SQLite 适合本地、单进程或低并发；PostgreSQL 适配不等于完整生产服务。
- 当前没有 FastAPI、异步 Worker、多租户、RBAC、GitHub PR 主链路和 OpenTelemetry 生产观测。
- Feature Hash 向量不具备真正语义理解；GLM Embedding/Rerank 会把代码发送到外部服务。
- Checkpoint 保存状态，但不保证外部副作用 exactly-once；写操作仍需要消费稳定幂等键。
- 测试通过只提高补丁可信度，不能替代覆盖率、静态检查、安全扫描和人工审查。

后续生产化路线以 [`docs/plans/production-evolution-plan.md`](docs/plans/production-evolution-plan.md) 为唯一计划文档。

## 代码与设计文档

```text
src/repo_agent/
  application.py            应用装配与 Explain 主链路
  maintenance_workflow/     Fix、反思、审批和回写状态图
  workflow/                 Diagnose Graph、Checkpoint、Evaluator、Final Answer
  react/                    结构化 ReAct Runtime
  tools/                    仓库工具、执行边界和 Tool Registry
  rag/                      分块、Embedding、检索、pgvector 与 Reranker
  memory/                   长期记忆、形成、治理与双后端 Store
  context_engineering/      Packet、信任分区、Token 预算和压缩
  skills/                   Skill 发现、路由、执行与版本快照
  mcp/                      MCP Gateway、HTTP/Registry Server 适配
  candidate/                候选工作副本、Patch、Evaluator 和 Promotion
  evals/                    离线评测模型与 Runner
```

关键设计入口：

- [`docs/adr`](docs/adr)：架构决策记录。
- [`docs/chains`](docs/chains)：主链路和状态流。
- [`docs/failures`](docs/failures)：真实失败模式与修复原则。
- [`docs/interview`](docs/interview)：按模块整理的设计讲解材料。
- [`docs/adr/012-langgraph-maintenance-loop.md`](docs/adr/012-langgraph-maintenance-loop.md)：独立维护工作流。
- [`docs/adr/013-final-answer-mcp-postgres-storage.md`](docs/adr/013-final-answer-mcp-postgres-storage.md)：Final Answer、MCP 主链路和双存储后端。

## 查看完整命令

```powershell
repo-agent --help
repo-agent explain --help
repo-agent fix --help
repo-agent resume-fix --help
repo-agent eval --help
```
`
