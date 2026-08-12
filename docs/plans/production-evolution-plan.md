# RepoAgent 生产化演进与长任务执行计划

> 文档日期：2026-08-07（持续维护）  
> 适用仓库：`D:\development\hello-agents\repo-agent`  
> 文档用途：交付给长任务 Agent，按顺序持续实现、测试和记录，不作为简历文案。  
> 默认基准：把 RepoAgent 演进为可长期维护的、多用户服务端代码维护 Agent；本地 CLI 继续作为开发和调试入口。
> **维护约束：本文件是唯一的生产化任务文档。后续阶段只更新本文件的状态、任务和验收结果，不再创建独立阶段计划。**
> **本机运行约束：Docker Engine 主要运行在 WSL2 中。开发环境的 PostgreSQL/pgvector、Redis、MinIO 等中间件以及 Docker Sandbox 默认部署到 WSL2 的 Linux 容器，不按 Windows Container 或必须依赖 Docker Desktop 的方式设计。**

## 1. 执行原则

1. 先保证 Agent 核心闭环正确，再扩展基础设施。
2. “存在类和单元测试”不等于“能力已接入主链路”。每个模块都必须具有真实入口、端到端测试和可观测结果。
3. 不编造成功率、召回率或性能数据。所有指标必须由固定评测集运行得到。
4. 保留本地 SQLite 实现作为开发后端，通过 Port 增加生产后端，不直接删除现有能力。
5. 不把用户私有仓库内容提交到 RepoAgent 仓库，也不在默认配置下发送源码到外部 Embedding 服务。
6. 所有新增代码注释、文档、报错信息优先使用中文；类名、字段名和协议术语保持英文。
7. 每完成一个阶段：先运行该阶段定向测试，再运行全部离线测试，最后更新本文档中的完成状态和实际结果。
8. 工作区已有修改属于用户。开始前检查 Git 状态，只修改本计划涉及的文件，不清理、不覆盖无关改动。
9. 不使用破坏性 Git 命令，不自动提交、不自动推送，除非用户另行授权。
10. 如果外部依赖不可用，可以完成 Port、适配器和离线测试，但必须把真实集成测试标记为未完成，不能用 Mock 结果冒充真实运行结果。

## 2. 当前系统的准确边界

### 2.1 已接入主链路

- 显式选择单个目标 Python 仓库，使用 `ProjectContext` 隔离不同项目。
- GLM/DeepSeek 结构化 Planner、ReAct Decision、Reflector。
- LangGraph `Plan -> Execute -> Evaluate -> Reflect -> Replan` 只读解释图。
- SQLite Checkpoint、线程隔离和恢复时仓库版本校验。
- SQLite FTS5 BM25、JSON 向量精确扫描、RRF 混合检索。
- 任务前 RAG/Memory 预检索和 Context Builder。
- 任务结束后的情景记忆、单任务语义提取、感知记忆和 Curator 治理。
- 两个本地 Skill 能力包及确定性路由。
- 独立 MaintenanceGraph，包含 Patch 生成、候选验证、失败反思、Repatch、持久化审批和安全写回。
- 正式 `evals/` 数据集、离线 Retrieval/Explain/Patch Runner 和 JSON 评测报告。

### 2.2 已实现组件但没有完整接入

- MCP Gateway 和 HTTP Client 没有在 `RepoAgentApplication` 或 CLI 中装配。
- `SemanticMemoryConsolidator` 没有调度入口、后台任务或 CLI 命令。
- 最终回答仍以工作流报告为主，没有独立的答案综合与逐条引用校验器。
- 评测集已经具备第一版，但规模和真实仓库覆盖仍有限，后续模块必须持续扩充。

### 2.3 明确未生产化

- 默认 256 维特征哈希不是语义 Embedding。
- RAG 和 Memory Dense Retrieval 都是 `O(N)` 精确扫描，没有 ANN 索引。
- 没有 PostgreSQL、pgvector/Qdrant、对象存储、任务队列和多实例运行。
- 没有容器级执行隔离；候选目录和路径沙箱不等于操作系统沙箱。
- 没有 FastAPI、认证、RBAC、多租户、配额和 GitHub/GitLab 集成。
- 没有真实 Token 流、模型重试/退避/熔断/限流和成本台账。
- 没有 OpenTelemetry、结构化日志、指标面板和运行回放。
- Python AST 只处理顶层类和函数，没有多语言解析、调用图和跨文件符号索引。

## 3. 目标架构

采用“模块化单体 + 可替换基础设施”的演进方式，暂不拆微服务。

```text
CLI / FastAPI / Git Provider Webhook
                |
        Application Service
                |
    +-----------+-------------------------------+
    |                                           |
DiagnoseGraph                            MaintenanceGraph
Plan/Execute/Evaluate                    Analyze/Propose/Evaluate
/Reflect/Replan/Answer                   /Reflect/Repatch/Approval/Promote
    |                                           |
    +-------------- Tool Registry --------------+
             |          |          |
       Repository     Skill      MCP Gateway
          Tools       Tools         Tools
             |
   ExecutionBackendPort
      |             |
LocalProcess     DockerSandbox

Persistence Ports
  |- CheckpointStore: SQLite / PostgreSQL
  |- MetadataStore: SQLite / PostgreSQL
  |- VectorStore: SQLiteExact / pgvector / Qdrant
  |- ArtifactStore: LocalFS / S3-Compatible
  `- EventStore: LocalLog / PostgreSQL
```

## 当前执行状态

- **P0 已完成：**已加入 `evals/` 夹具、严格 JSONL Loader、Retrieval/Explain/Patch Runner、JSON 报告和 `repo-agent eval ...` CLI。
- **P1 已完成：**已加入独立 MaintenanceGraph、Port、SQLite Checkpoint、失败 Patch 反思与 Repatch、可恢复审批、`resume-fix` 和旧 `apply` 兼容入口。
- **P2 已完成：**已接入 `FinalAnswerSynthesizerPort`、逐条引用复核、MCP 配置装配到 Application/CLI/Tool Registry、本地 Registry MCP Server 契约测试、Memory Consolidation 显式 CLI 与幂等归纳记录。
- **P3 已完成：**用户已确认第二阶段交付完成，本轮按要求不重复验收；具体实现、测试数量和真实性边界以执行 Agent 回写的记录为准。
- **当前下一阶段：P4-P7 后端闭环。**目标是在不开发前端、不引入 Kubernetes 的前提下，完成执行安全、代码智能、服务化、多租户、异步任务、Git 平台、可观测性和可靠性，使 RepoAgent 成为可通过 API 使用的完整后端系统。

### P2/P3 本次验收记录（2026-08-08）

- 定向测试：`.\.venv\Scripts\python.exe -m unittest tests.test_production_evolution_p2_p3 -v`，4 通过 / 0 失败 / 0 跳过。
- 受影响离线测试：`.\.venv\Scripts\python.exe -m unittest tests.test_cli tests.test_application tests.test_mcp_gateway tests.test_mcp_http tests.test_memory_and_context tests.test_memory_curation tests.test_repository_rag tests.test_production_evolution_p2_p3 -v`，90 通过 / 0 失败 / 0 跳过。
- 全量离线测试：`.\.venv\Scripts\python.exe -m unittest discover -s tests -v`，239 通过 / 0 失败 / 3 跳过。
- Eval Retrieval：`.\.venv\Scripts\python.exe -m repo_agent eval retrieval --dataset evals/retrieval/python-small.jsonl`，5 case 通过，Mean Recall@K = 1.0，最低 MRR = 0.3333333333333333。
- Eval Explain：`.\.venv\Scripts\python.exe -m repo_agent eval explain --dataset evals/explain/python-small.jsonl`，2 case 通过。
- Eval Patch：`.\.venv\Scripts\python.exe -m repo_agent eval patch --dataset evals/patch/python-small.jsonl`，2 case 通过，patch_attempts = 2。
- 迁移 dry-run：`.\.venv\Scripts\python.exe -m repo_agent migrate-state --sqlite-state-dir output\my-state --postgres-dsn postgresql://repo_agent:secret@localhost/repo_agent --dry-run`，报告 RAG 78 chunks / 23 files、Memory 1 条、Memory curation 1 条、Checkpoint 未迁移。
- 编译检查：`.\.venv\Scripts\python.exe -m py_compile src/repo_agent/application.py src/repo_agent/cli.py src/repo_agent/storage.py src/repo_agent/migration.py src/repo_agent/rag/postgres.py src/repo_agent/memory/postgres.py src/repo_agent/workflow/runtime_factory.py src/repo_agent/workflow/runtime_postgres.py src/repo_agent/workflow/final_answer.py`，通过。
- 本地 MCP 示例配置烟测：`configs/mcp.local-registry.example.json` 已通过真实 Registry MCP Server 装配，映射工具 `mcp_local_list_files`。
- 真实 PostgreSQL 集成：未执行；`docker --version; docker compose version` 失败，当前环境没有 Docker 命令，也未提供已迁移 PostgreSQL/pgvector DSN。未报告 ANN 性能、P50/P95 或 1k/10k/100k 基准。

### 3.1 生产后端的默认技术选择

- 关系数据、任务、Memory 元数据：PostgreSQL。
- 第一版向量后端：pgvector，使用 HNSW；保留 SQLite 精确扫描后端。
- Checkpoint：LangGraph PostgreSQL Saver。
- 异步任务：Redis + Worker，具体框架在服务化阶段再定，不提前绑定业务层。
- Artifact：开发环境使用本地文件，生产接口兼容 S3/MinIO。
- 执行隔离：一次任务一个临时 Docker 容器，默认禁网、非 root、资源受限。
- 可观测性：结构化日志 + OpenTelemetry Trace/Metrics。

选择 pgvector 而不是立刻上专用向量数据库的原因：当前项目规模和团队规模有限，Chunk/Memory 元数据天然需要关系查询；pgvector 能先解决 ANN、过滤和并发问题，同时降低运维复杂度。达到独立扩缩容、超大规模向量或更复杂过滤需求后，再实现 Qdrant Adapter。

## 4. 总体里程碑

| 阶段 | 目标 | 状态 |
|---|---|---:|
| P0 | 建立真实评测基线 | 已完成 |
| P1 | 实现真正的 LangGraph FixGraph | 已完成 |
| P2 | 接入 Final Answer、MCP 主链路和 Memory 慢路径 | 已完成 |
| P3 | PostgreSQL/pgvector、双后端存储与迁移 | 已完成（用户确认，本轮未复验） |
| P4 | Docker 执行沙箱 | 下一阶段 |
| P5 | 多语言代码智能和代码图 | 下一阶段 |
| P6 | FastAPI、异步 Worker、多租户和 Git 平台 | 下一阶段 |
| P7 | 可观测性、可靠性和安全治理 | 下一阶段 |

## 5. P0/P1 历史交付范围（已完成）

本节保留 P0/P1 的原始交付定义，作为设计和验收历史，不再作为当前 Agent 的执行任务。当前任务以第 8、9、18 节为准。

### 5.1 今晚必须交付

- 正式的 `evals/` 目录、数据格式、评测 Runner 和 JSON 报告。
- 至少一个只读代码定位评测集和一个 Patch 修复评测集。
- 一个独立的 `RepoAgentMaintenanceWorkflow`。
- Patch 首次失败后，测试证据能够进入 Reflect，再生成第二版 Patch。
- 维护任务能够在 SQLite Checkpoint 中恢复。
- 人工审批成为持久化工作流状态，或至少实现兼容当前 LangGraph 版本的 interrupt/resume；不得只保留图外 `apply` 作为最终方案。
- CLI 的 `fix` 使用新的维护工作流。
- 旧 proposal 加载和 `apply` 命令保持兼容，除非提供数据迁移和兼容说明。
- 定向测试和全部离线测试通过。
- 新增 ADR、流程文档和失败案例，说明设计取舍。

### 5.2 今晚禁止顺手扩张

- 不引入 Kubernetes。
- 不实现 Web 前端。
- 不同时引入 PostgreSQL、Qdrant、Redis、MinIO。
- 不重写现有 DiagnoseGraph。
- 不删除 SQLite、本地 CLI 或现有 proposal 文件。
- 不为了“架构统一”大范围改名。
- 不用 LLM Judge 代替编译和 pytest。
- 不把用户提供的医院仓库复制进测试夹具。

### 5.3 今晚建议停止点

当以下命令和验收全部通过后停止，不自动进入 P3：

```powershell
python -m unittest discover -s tests -v
repo-agent eval retrieval --dataset evals/retrieval/python-small.jsonl
repo-agent eval patch --dataset evals/patch/python-small.jsonl
```

如果 CLI 命令名称因现有结构需要调整，可以修改，但必须在 README 和本文档中记录最终命令。

## 6. P0：评测基线的具体实现

### 6.1 目标

在替换 Embedding、向量后端、Reranker 或工作流之前，先能够量化当前系统。评测层必须区分：

- Retrieval 是否召回正确证据。
- Agent 是否定位到正确文件和符号。
- 最终回答是否包含有效引用。
- Patch 是否修复目标测试。
- Patch 是否保持回归测试通过。
- 运行耗时、LLM 调用次数和 Token 是否可接受。

### 6.2 目录设计

新增：

```text
evals/
  README.md
  fixtures/
    calculator_repo/
    layered_service_repo/
    failing_pytest_repo/
  retrieval/
    python-small.jsonl
  explain/
    python-small.jsonl
  patch/
    python-small.jsonl
  baselines/
    current-local.json
src/repo_agent/evals/
  __init__.py
  models.py
  loader.py
  retrieval_runner.py
  explain_runner.py
  patch_runner.py
  report.py
tests/
  test_eval_dataset.py
  test_eval_runners.py
```

夹具必须是最小、可提交、无隐私、无网络依赖的 Python 项目。

### 6.3 数据模型

在 `src/repo_agent/evals/models.py` 使用严格 Pydantic 模型：

```text
RetrievalEvalCase
  case_id
  repo_fixture
  query
  relevant_paths
  relevant_symbols
  relevant_line_ranges
  top_k

ExplainEvalCase
  case_id
  repo_fixture
  question
  required_paths
  required_claims
  forbidden_claims

PatchEvalCase
  case_id
  repo_fixture
  objective
  target_tests
  regression_tests
  expected_changed_paths
  forbidden_changed_paths
```

所有 ID 唯一；路径必须是规范相对路径；JSONL 加载时拒绝额外字段。

### 6.4 Retrieval Runner

复用现有 `evaluate_retrieval`，但扩展为 Chunk/符号级评测：

- `Recall@K`：相关 Chunk 或路径被召回的比例。
- `MRR`：第一条相关结果排名。
- `Hit@K`：是否至少命中一个相关结果。
- `Citation Accuracy`：返回路径和行号是否仍对应当前源码。
- 按检索模式分别运行 lexical、dense、hybrid。

报告必须包含每个 Case 的原始命中，不只保留宏平均值。

### 6.5 Explain Runner

分成两个层次：

1. 离线确定性测试：使用 Scripted LLM，验证上下文、工具调用和引用链。
2. 真实模型评测：需要显式环境变量才运行，输出结果但不进入默认单元测试。

第一版不使用 LLM Judge。通过确定性规则检查：

- 必需路径是否出现在 Evidence。
- 引用的行号是否合法。
- 是否出现禁止断言。
- Workflow 是否通过 Evaluator。

### 6.6 Patch Runner

每个 Case 使用全新临时仓库副本：

- 先运行目标测试，确认基线确实失败。
- 运行维护工作流。
- 验证目标测试由失败变为通过。
- 验证回归测试继续通过。
- 验证修改范围。
- 不自动回写原夹具。

真实 LLM Patch 评测必须单独标记，避免默认测试产生费用。

### 6.7 最小运行指标

新增一个供应商无关的 `RunMetrics`：

- `duration_ms`
- `llm_requests`
- `tool_calls`
- `prompt_tokens`，供应商不返回时为 `null`
- `completion_tokens`，供应商不返回时为 `null`
- `estimated_cost`，没有价格配置时为 `null`
- `rag_queries`
- `memory_queries`
- `patch_attempts`

不得把未知值写成零。

### 6.8 P0 验收标准

- 数据集错误会在执行前报告明确错误。
- 相同本地 Embedding 和固定夹具重复运行结果稳定。
- 报告输出 JSON，可被后续版本对比。
- 至少 5 个 Retrieval Case、2 个 Explain Case、2 个 Patch Case。
- 测试覆盖空数据集、坏路径、重复 ID、非法行号和基线测试未失败。
- README 说明离线评测与付费真实模型评测的区别。

## 7. P1：真正的 LangGraph FixGraph

### 7.1 设计决策

新增独立 `RepoAgentMaintenanceWorkflow`，不要把所有维护字段硬塞进现有 DiagnoseGraph。两张图共享 Port、工具、Context、RAG、Memory 和 LLM Adapter。

第一版允许复用现有 `RepoAgentApplication.explain` 产生只读分析结果，但 Patch 生成、验证、反思、重试、审批和回写必须全部进入维护图。后续再把 DiagnoseGraph 作为 Subgraph 内联。

### 7.2 新增目录建议

```text
src/repo_agent/maintenance_workflow/
  __init__.py
  models.py
  ports.py
  graph.py
  runtime.py
  evaluators.py
  adapters.py
tests/
  test_maintenance_workflow.py
  test_maintenance_checkpoint.py
  test_maintenance_cli_e2e.py
docs/adr/
  012-langgraph-maintenance-loop.md
docs/chains/
  maintenance-workflow.md
docs/failures/
  patch-test-failure-without-repatch.md
```

如果执行者认为放在现有 `workflow/` 更合理，可以调整目录，但必须保持 Diagnose 与 Maintenance 的状态模型分离。

### 7.3 状态模型

`MaintenanceGraphState` 至少包含：

```text
run_id
thread_id
project_id
repo_root
repo_revision
objective
analysis_report
analysis_evidence
selected_targets
patch
patch_history
patch_attempt
evaluation
evaluation_history
reflection
reflection_history
proposal_id
approval_status: pending | approved | rejected
promotion_result
status: running | waiting_approval | completed | failed
stop_reason
trace
```

不要把 API Key、完整环境变量或不可序列化对象写入 State。

### 7.4 节点设计

```text
START
  -> analyze_repository
  -> select_targets
  -> propose_patch
  -> evaluate_patch
      -> passed: persist_proposal
      -> failed and attempts remain: reflect_patch
      -> failed and attempts exhausted: report_failure
  -> reflect_patch
      -> repair: propose_patch
      -> reselect: select_targets
      -> stop: report_failure
  -> persist_proposal
  -> await_approval
      -> approved: promote_patch
      -> rejected: report_rejected
  -> promote_patch
  -> report_success
  -> END
```

### 7.5 节点职责

#### analyze_repository

- 只读执行。
- 复用 RAG、Memory、Skill 和 ReAct。
- 输出结构化 `RepositoryAnalysis`，不能只保存长字符串。
- 至少包含相关文件、符号、引用、风险和建议测试。

#### select_targets

- 基于 objective 和 analysis 选择允许修改的文件。
- 读取当前文件哈希。
- 生成目标测试和回归测试列表。
- 文件范围和测试目标进入后续不可绕过的确定性约束。

#### propose_patch

- 第一次使用 objective、analysis 和目标文件内容。
- 重试时额外输入上一版 Patch、测试失败、Reflection 和当前文件内容。
- 继续使用严格 Pydantic Schema。
- Patch 必须带 `expected_sha256`。
- 不允许模型扩大文件范围；扩大范围必须回到 `select_targets`。

#### evaluate_patch

- 每次尝试创建新的 Candidate Workspace。
- 应用 Patch。
- 顺序执行 change scope、compile、target tests、regression tests。
- 整个节点完成后把结构化 Evaluation 和 diff 写入 State。
- Candidate Workspace 可以在节点结束时清理，因为下一轮可以从真实仓库基线重新创建并应用新 Patch。

#### reflect_patch

- 只消费客观 Evaluation。
- 结构化输出：`failure_kind`、`corrective_action`、`next_action`。
- `next_action` 只能为 `repair`、`reselect`、`stop`。
- 不允许 Reflector 把失败改写成成功。

#### persist_proposal

- 复用 `MaintenanceProposal`。
- 增加 Patch/Evaluation 历史或建立版本化 Artifact 引用。
- 使用原子写入。
- 同一个 execution key 重放时不得生成第二个 proposal。

#### await_approval

- 优先使用当前 LangGraph 版本支持的 `interrupt()`/`Command(resume=...)`。
- 审批状态必须进入 Checkpoint。
- CLI 输出 proposal id、diff、测试摘要和恢复命令。
- 审批输入必须明确是 approved 或 rejected，不接受模糊自然语言直接写回。

#### promote_patch

- 再次校验 project id、repo revision 和每个文件旧哈希。
- 只有 Evaluation passed 且 approval approved 才能运行。
- 重放时使用稳定 execution key；已经成功写回则返回已有审计结果。
- 复用当前原子替换与回滚逻辑。

### 7.6 Port 设计

新增或重构以下 Port，Graph 不直接创建具体客户端：

```text
RepositoryAnalyzerPort.analyze(...)
PatchTargetSelectorPort.select(...)
PatchProposerPort.propose(...)
PatchEvaluatorPort.evaluate(...)
PatchReflectorPort.reflect(...)
ProposalStorePort.save/load/find_by_execution_key(...)
PatchPromoterPort.promote(...)
```

已有类优先通过 Adapter 实现 Port，避免复制逻辑：

- `StructuredCandidatePatchGenerator`
- `ObjectiveCandidateEvaluator`
- `CandidatePatchPromoter`
- `RepoAgentMaintenanceService`

### 7.7 重试与停止条件

- 默认 `max_patch_attempts=3`。
- 相同 Patch 哈希不得重复评估。
- 相同失败且没有新证据时停止，避免无效循环。
- 仓库 revision 在审批前变化时失败，不自动把旧 Patch 应用到新版本。
- Schema 错误可以有限重试；权限错误、哈希冲突和未授权路径不得盲目重试。

### 7.8 CLI 改造

目标行为：

```powershell
repo-agent fix --repo D:\code\target "修复目标" --allow-code-execution
```

运行到审批节点后输出：

- `thread_id`
- `proposal_id`
- Patch diff
- 目标测试和回归测试摘要
- `waiting_approval`

恢复并批准：

```powershell
repo-agent resume-fix --thread-id <id> --approve
```

拒绝：

```powershell
repo-agent resume-fix --thread-id <id> --reject
```

保留现有：

```powershell
repo-agent apply --proposal-id <id> --approve
```

它作为兼容入口，但 README 应推荐持久化工作流审批入口。

### 7.9 P1 必须覆盖的测试

1. 第一版 Patch 目标测试失败，第二版 Patch 修复成功。
2. Reflection 收到真实 pytest stdout/stderr，而不是模型自述。
3. Patch 修改了未授权文件时直接失败。
4. 相同 Patch 指纹不会无限重试。
5. 达到最大尝试次数后明确失败。
6. 通过测试后停在 `waiting_approval`，真实仓库未变化。
7. 关闭 SQLite 连接并创建全新 Runtime 后，可以恢复审批。
8. 批准后只写回允许文件。
9. 拒绝后真实仓库不变。
10. 等待审批期间仓库发生变化，恢复后拒绝写回。
11. promotion 节点重放不会重复产生副作用。
12. 旧 proposal 文件仍可由 `apply` 加载。

### 7.10 P1 完成定义

- CLI `fix` 不再调用“图结束后的单次 Patch 管道”作为主实现。
- 测试失败能够驱动至少一次结构化 Reflection 和 Repatch。
- 审批状态可恢复。
- Graph State 可以完整序列化往返。
- README 的流程描述与代码一致。
- 新增流程图、ADR 和失败案例。
- 全部离线测试通过。

## 8. P2：补齐现有半集成模块（已完成）

本节保留 P2 的交付定义，作为设计和验收历史。

### 8.1 最终答案生成与引用校验

新增 `FinalAnswerSynthesizerPort`：

- 输入只能来自已经通过 Evaluator 的 Step Results 和 Evidence。
- 输出包含 answer、claims、citations、limitations。
- 每条引用重新通过 `read_file_range` 校验 revision、路径和行号。
- 无引用的重要断言标记为 unsupported，不能悄悄进入答案。
- 最终 Answer 失败不应推翻客观 Workflow 状态，但必须报告生成失败。

### 8.2 MCP 主链路装配

- 增加本地 MCP Server 配置文件和严格 Pydantic 配置模型。
- `RepoAgentApplicationConfig` 接收 MCP 配置路径。
- Application 启动时 discover/list tools，经 Host Policy 审核后注册到当前 Tool Registry。
- MCP 工具必须继续经过当前 Step allowed_tools、显式授权和结果大小限制。
- Checkpoint 保存 MCP 能力目录哈希，恢复时校验漂移。
- 增加一个真实、可本地启动的测试 MCP Server 做契约测试。
- 后续再增加官方 SDK/stdio、OAuth、Resources 和 Prompts。

### 8.3 Memory 慢路径调度

第一版先增加显式 CLI，不立刻上分布式队列：

```powershell
repo-agent memory consolidate --project <name> --topic "测试失败模式"
```

然后增加调度策略：

- 同项目同主题积累至少 N 条 active verified episodic memory。
- 最近一次 consolidation 后出现足够的新证据。
- 使用稳定 consolidation key 防重。
- 输出候选仍然经过 Curator，不能直接写入 verified。
- 保存输入 episode ids、模型版本、Prompt 版本和归纳结果。

## 9. P3：生产级知识与持久化层（已完成）

### 9.1 前置条件

- P0 已经有稳定检索基线。
- RAG、Memory 的调用方不再直接依赖 SQLite 类。
- Docker 环境可运行 PostgreSQL + pgvector。

### 9.2 Port 抽象

新增：

```text
RAGIndexPort
  index_repository(context) -> IndexingReport
  search(context, query, top_k, mode) -> RetrievalResult
  delete_project(project_id)
  close()

MemoryStorePort
  create/replace/search/get/forget/expire/...

CheckpointRuntimeFactory
  create(...)
```

将 Application 中的 `SQLiteRAGIndex`、`SQLiteMemoryStore` 和 `SQLiteWorkflowRuntime` 创建逻辑移到基础设施 Factory。

### 9.3 PostgreSQL Schema

使用正式迁移工具管理 Schema，不能依赖应用启动时临时建表。

核心表：

- `projects`
- `repository_index_state`
- `repository_files`
- `repository_chunks`
- `memories`
- `memory_evidence`
- `memory_lifecycle_events`
- `maintenance_proposals`
- `maintenance_attempts`
- `run_events`

`repository_chunks` 至少包含：

```text
chunk_id
project_id
repo_revision
path
start_line
end_line
kind
symbol
content
content_hash
embedding_model
embedding_dimensions
embedding vector(dim)
search_document
created_at
updated_at
```

索引：

- `(project_id, repo_revision)` B-Tree。
- `(project_id, path)` B-Tree。
- `embedding` HNSW。
- 关键词检索使用 PostgreSQL FTS，并为代码标识符补充 trigram/token 字段。

### 9.4 混合检索

- Lexical 和 Dense 各自扩大候选池。
- 使用现有 RRF 作为第一版融合算法。
- 元数据过滤必须在 ANN 查询阶段生效，至少过滤 project、revision、embedding space。
- 批量写入 Embedding，避免逐 Chunk 网络调用。
- 删除文件时在同一事务内更新文件、Chunk 和索引状态。

### 9.5 Reranker

只有在评测证明 Hybrid 仍存在排序问题后才实现：

- 定义 `RerankerPort`。
- 输入 Top-N 候选，输出稳定排序和评分说明。
- Reranker 超时或失败时回退到 RRF。
- 记录额外耗时和 Token。
- 使用 P0 数据集进行有无 Reranker 对照，不预设它一定提高效果。

### 9.6 P3 验收

- 同一评测集可分别运行 SQLite 和 pgvector 后端。
- PostgreSQL 结果满足项目和 revision 隔离。
- ANN 查询不再在 Python 中读取全部向量。
- 增量索引、删除、重建、Embedding 空间变化均有集成测试。
- 至少进行 10 万 Chunk 的构造型性能测试并记录 P50/P95，不编造数据。
- SQLite 后端仍通过原有离线测试。

### 9.7 PostgreSQL Memory 后端

- `MemoryStorePort` 覆盖现有 create、replace、search、get、forget、expire、生命周期事件和重建 Embedding 空间能力。
- 表至少包含 memories、memory_evidence、memory_lifecycle_events、memory_embedding_state 和 memory_consolidation_runs。
- 同项目 active memory_key 使用部分唯一索引；状态和 claim status 使用数据库约束。
- 遗忘、过期、替代和生命周期事件必须在事务中完成，墓碑不可被普通搜索召回。
- 检索必须在数据库中完成 project/type/claim/importance/TTL/revision 过滤，再执行 FTS、pgvector ANN 和 RRF；禁止把全部 Memory 向量读入 Python。
- P2 的 Memory Consolidation CLI 同时兼容 SQLite 和 PostgreSQL。

### 9.8 PostgreSQL Checkpoint 与持久化幂等

- 增加统一 Checkpoint Runtime Factory，DiagnoseGraph 和 MaintenanceGraph 使用相同后端配置。
- PostgreSQL 模式使用 LangGraph PostgreSQL Saver，保持 project namespace 和逻辑 thread id 隔离。
- waiting approval 可以由另一个进程恢复，批准前重新校验仓库 revision。
- promotion execution key 写入持久化幂等表；成功节点重放时返回已有结果，不重复修改仓库。
- 增加 checkpoint 查看、保留和清理能力，不自动删除仍在等待审批的任务。

### 9.9 双后端配置与迁移

- 增加 `StorageConfig` 和 Factory，支持 `sqlite|postgres`，配置优先级为 CLI > 环境变量 > 本地默认值。
- PostgreSQL 缺少 DSN 或 Schema 版本过旧时早失败，日志不得输出密码。
- SQLite 继续是零外部依赖的默认开发模式，PostgreSQL 依赖使用可选依赖组。
- 提供 Docker Compose PostgreSQL/pgvector、healthcheck、`.env.example` 和正式 Alembic 迁移。
- 提供 `migrate-state --dry-run/--execute`，迁移 Projects、RAG、Memory、Proposal 和审计数据；无法可靠迁移的 Checkpoint 必须明确说明，不能猜测转换。
- 迁移只读 SQLite 源、目标事务化、重复执行幂等，不删除源文件，不在日志输出源码或 Memory 正文。

### 9.10 契约测试与真实验证

- 同一套 RAG Contract、Memory Contract 和 Checkpoint Contract 分别运行 SQLite/PostgreSQL。
- 默认测试不依赖 Docker；PostgreSQL 集成测试和真实 Embedding 测试必须显式开启。
- Eval Runner 输出 storage_backend、embedding_model、reranker、索引耗时、查询 P50/P95、Recall@K、MRR 和降级次数。
- 构造 1,000、10,000、100,000 Chunk 基准，记录全量索引、1% 增量索引、Dense/Hybrid P50/P95 和存储大小。
- 只报告真实环境数据；资源不足时记录实际停止规模和原因。
- 增加 PostgreSQL 本地开发、状态迁移、并发索引、Embedding 空间不匹配和 promotion replay 文档。

## 10. P4：Docker 执行沙箱

新增 `ExecutionBackendPort`：

```text
execute(command, workspace, limits, network_policy, env_policy) -> ProcessResult
```

实现：

- `LocalProcessBackend`：保留当前开发行为。
- `DockerExecutionBackend`：生产默认。

本机开发环境以 WSL2 Docker Engine 为准：

- 优先让 API、Worker 和中间件整体运行在同一个 WSL2 Docker Compose 网络中。
- PyCharm 调试优先使用 WSL Python Interpreter；如果 API 临时运行在 Windows Python 中，则通过发布到 `localhost` 的端口访问 WSL 中的 PostgreSQL、Redis 和 MinIO。
- 不要求在 Windows 上额外安装或启动一套 PostgreSQL、Redis、MinIO，避免形成两套状态源。
- 不通过未加密的 Docker TCP 端口暴露 WSL Docker Daemon。
- Docker Sandbox 的创建由 WSL 内的 Worker 或受控 Sandbox Adapter 完成；Windows 进程不得假定可以直接访问 `/var/run/docker.sock`。
- 仓库源码可从 `/mnt/d/...` 访问，但高频索引、依赖安装和 Candidate Workspace 优先放在 WSL ext4 文件系统或 Docker Volume，避免跨文件系统 I/O 成为主要瓶颈。
- Windows 路径、WSL 路径和容器路径必须通过统一的 `WorkspacePathMapper` 显式转换，禁止在业务层散落字符串替换。

Docker 约束：

- 非 root 用户。
- 只挂载 Candidate Workspace，不挂载用户主目录和 RepoAgent 状态目录。
- 默认 `--network none`。
- CPU、内存、PID、磁盘和超时限制。
- 最小环境变量，不透传 API Key。
- 容器退出后强制清理。
- 镜像按 Python 版本和依赖锁文件缓存。

测试至少验证：

- 恶意测试不能读取候选目录外的宿主文件。
- 默认不能访问网络。
- 无限循环会超时。
- 内存或进程数超限会失败并生成结构化错误。
- 任务结束后无残留容器。

## 11. P5：代码智能深化

按以下顺序实现，不一次完成全部语言：

1. Python 类方法单独分块，保留父类 Chunk 引用。
2. Python import、定义、调用关系图。
3. Parent-Child Retrieval：先召回符号，再补父模块或相邻上下文。
4. 使用 Tree-sitter 抽象 `LanguageParserPort`。
5. 增加 Java，复用用户已有后端经验验证跨语言设计。
6. 后续接入 LSP/SCIP，补充跨文件定义和引用。
7. 把符号图作为独立检索源与 BM25/Dense 融合。

每增加一种检索源都必须扩展 P0 评测集。

## 12. P6：服务化与多用户

### 12.1 API

提供：

- 项目注册和 Git 仓库导入。
- 创建 Diagnose/Fix 任务。
- 查询任务状态。
- SSE/WebSocket 订阅运行事件。
- 审批或拒绝 Patch。
- 查看 diff、测试报告和引用。
- Memory 查询、审核、遗忘。

### 12.2 异步任务

- API 只创建任务，不在请求线程运行 Agent。
- Worker 获取任务租约并运行 Graph。
- 支持取消、超时、重试和 Worker 崩溃恢复。
- 使用 Outbox 或等价机制保证任务状态与事件一致。

### 12.3 多租户

所有持久化实体增加：

- `tenant_id`
- `user_id`
- `project_id`

权限至少区分：

- 查看仓库。
- 运行解释任务。
- 允许代码执行。
- 审批写回。
- 管理 Memory。
- 安装 Skill/MCP Server。

不能只靠 Prompt 或前端隐藏按钮实现权限。

### 12.4 Git 平台

- 使用 GitHub/GitLab App 或 OAuth，不长期保存个人明文 Token。
- Clone 到受控 Workspace。
- Webhook 触发 revision 更新和增量索引任务。
- 修复结果默认创建分支和 Pull Request，不直接写默认分支。
- PR 描述包含目标、修改、测试、风险和引用。

## 13. P7：可观测性、可靠性和安全治理

### 13.1 运行事件

定义统一事件：

```text
run.started
node.started
node.completed
llm.requested
llm.completed
tool.requested
tool.completed
rag.searched
memory.searched
patch.proposed
patch.evaluated
approval.requested
approval.resolved
run.completed
run.failed
```

事件包含 run/thread/project/node/attempt，但不得包含 API Key 和未经处理的敏感源码。

### 13.2 LLM 可靠性

- 只对连接错误、超时和部分 429 做有限指数退避。
- 401、Schema 错误和权限错误不得盲目重试。
- 加入供应商并发限制和熔断。
- 记录 request id、模型、Token、耗时和估算费用。
- 增加模型路由：规划/反思可用强模型，分类/压缩可用小模型。

### 13.3 数据安全

- Secret Manager 代替长期环境变量。
- PostgreSQL、对象存储和备份加密。
- 外部 Embedding 必须经过仓库级数据外发策略。
- 增加敏感信息检测、日志脱敏和审计。
- Memory 删除需要传播到向量索引、全文索引、Artifact 和备份策略。

## 14. 预计修改文件清单

今晚 P0/P1 优先涉及：

```text
pyproject.toml
README.md
src/repo_agent/cli.py
src/repo_agent/application.py
src/repo_agent/maintenance.py
src/repo_agent/candidate/generation.py
src/repo_agent/candidate/evaluator.py
src/repo_agent/candidate/promotion.py
src/repo_agent/workflow/checkpoints.py
src/repo_agent/evals/*
src/repo_agent/maintenance_workflow/*
tests/test_eval_dataset.py
tests/test_eval_runners.py
tests/test_maintenance_workflow.py
tests/test_maintenance_checkpoint.py
tests/test_maintenance_cli_e2e.py
evals/*
docs/adr/012-langgraph-maintenance-loop.md
docs/chains/maintenance-workflow.md
docs/failures/patch-test-failure-without-repatch.md
```

不要机械地创建所有文件；能够复用现有模块时优先复用，但不得通过把所有逻辑继续塞进 `maintenance.py` 来回避分层。

## 15. 每阶段验证命令

优先使用项目虚拟环境：

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

真实模型测试必须显式开启，默认测试不能消耗 Key：

```powershell
$env:RUN_LIVE_LLM_TESTS="true"
.\.venv\Scripts\python.exe -m unittest tests.test_glm_live -v
```

如果新增 PostgreSQL 集成测试，使用独立标记和独立数据库，不让默认离线测试依赖正在运行的外部服务。

## 16. 长任务 Agent 的执行报告格式

每完成一个阶段，在最终报告中按以下格式输出：

```text
阶段：P0 / P1
状态：完成 / 部分完成 / 阻塞

已实现：
- ...

未实现：
- ...

关键设计：
- ...

测试：
- 命令
- 通过数 / 失败数 / 跳过数

真实边界：
- 哪些只通过 Mock
- 哪些完成了真实端到端验证

下一步：
- ...
```

如果出现失败，必须保留失败证据并说明根因，不得为了显示绿色结果而删除测试、放宽断言或把异常吞掉。

## 17. P4-P7 后端闭环的完成边界

这一阶段的目标不是宣称“企业级能力全部完成”，而是交付一套可本地部署、可通过 API 真实使用、具备安全执行和异步任务能力的后端 MVP。完成后应支持以下完整链路：

```text
用户认证
  -> 注册本地仓库或导入 GitHub 仓库
  -> 后台增量索引
  -> 创建 Explain/Fix 任务
  -> Worker 恢复并运行 LangGraph
  -> SSE 获取节点、工具、检索、测试和审批事件
  -> Docker 沙箱执行代码与测试
  -> Fix 任务等待用户审批
  -> 批准后创建分支并提交变更
  -> 可选创建 Pull Request
  -> API 返回答案、引用、Diff、测试报告和审计记录
```

### 17.1 本阶段必须完成

- Docker 执行沙箱以及本地执行后端的统一 Port。
- Python 深层代码结构、调用关系图和 Java 第一版解析。
- FastAPI、OpenAPI、统一错误模型和 SSE 事件流。
- PostgreSQL 持久化任务、项目、审批、事件、权限和审计数据。
- Redis + Worker 异步执行、任务租约、取消、超时、重试和崩溃恢复。
- LocalFS/S3-Compatible Artifact Store，保存 Diff、测试报告和大体积输出。
- 用户、租户、项目级 RBAC，所有查询强制带租户边界。
- GitProviderPort、本地 Git Adapter 和 GitHub App/Token Adapter；完成分支、提交和 Pull Request 主链路。
- OpenTelemetry、结构化日志、Prometheus 指标、LLM/Tool/RAG/Memory 成本和耗时记录。
- Docker Compose 本地部署和真实端到端测试。

### 17.2 本阶段明确不做

- 不开发 Web 前端、桌面端或 IDE 插件。
- 不引入 Kubernetes、服务网格或自动扩缩容平台。
- 不同时实现 GitLab、Gitee 等多个平台；先完成 GitHub，其他平台只保留 Port。
- 不接入 Qdrant；继续使用 PostgreSQL + pgvector。
- 不追求所有编程语言；本阶段只要求 Python 完整、Java 第一版可用。
- 不实现组织级计费系统；只记录 Token、模型调用和可配置预算。
- 不允许为了赶进度绕过沙箱、租户隔离、审批或客观测试。

### 17.3 后端完成定义

只有同时满足以下条件，才能把本阶段标为完成：

1. `docker compose up` 能启动 API、Worker、PostgreSQL/pgvector、Redis 和 MinIO，并通过健康检查。
2. Explain 与 Fix 都从 API 创建并由 Worker 异步运行，不在 HTTP 请求线程内执行模型或测试。
3. Fix 在 Docker 沙箱中验证，测试通过后停在持久化审批状态；API 进程和 Worker 重启后仍可恢复。
4. 两个租户使用同名 project/thread 时数据仍完全隔离，越权请求返回 403，不能靠前端或 Prompt 隔离。
5. GitHub 仓库能够导入、增量索引、创建修复分支并生成 Pull Request；真实写操作必须经过明确审批。
6. SSE 能重放已持久化事件并继续接收新事件，断线重连不丢失终态。
7. 关键日志、Trace 和指标中不出现 API Key、Git Token、完整源码或未脱敏 Memory 正文。
8. Python 和 Java 夹具都能通过符号/调用关系辅助回答架构或调用链问题，并带有效源码引用。
9. 默认离线测试、PostgreSQL/Redis/MinIO 集成测试、Docker 沙箱安全测试和 API 端到端测试全部有真实结果。
10. README、OpenAPI 使用说明、部署文档、ADR、威胁模型和故障恢复手册与实际代码一致。

## 18. 可直接交给长任务 Agent 的下一阶段任务指令

```text
请完整阅读 docs/plans/production-evolution-plan.md。该文件是唯一生产化任务文档；不要新建任何阶段计划、交付计划或 TODO 文档。P0-P3 已完成，本次连续完成 P4-P7 的“后端闭环阶段”。不要重复验收上一阶段，也不要重写已经稳定的 RAG、Memory、Checkpoint 和 MaintenanceGraph；只在新接口接入确有必要时做兼容重构。

总体目标：
把 RepoAgent 从 CLI/单机工作流演进为一套可本地部署、可通过 API 真实使用的完整后端 MVP。最终用户能够注册本地或 GitHub Python/Java 仓库，创建 Explain/Fix 任务，通过 SSE 查看过程，Fix 在 Docker 沙箱中验证，用户批准后创建分支并可创建 Pull Request。所有任务、事件、审批、Memory、索引和审计状态都能持久化并在进程重启后恢复。

必须严格按以下顺序实施，每一部分完成后运行定向测试，再进入下一部分：

第一部分：P4 Docker 执行沙箱
1. 定义 ExecutionBackendPort、ExecutionRequest、ExecutionLimits、NetworkPolicy、EnvironmentPolicy 和结构化 ProcessResult；现有 subprocess 逻辑迁移到 LocalProcessBackend，仅用于显式开发模式。
2. 实现 DockerExecutionBackend。容器必须非 root、只读根文件系统、默认禁网、限制 CPU/内存/PID/执行时间、只挂载单次 Candidate Workspace，不挂载宿主仓库、状态目录、Docker Socket、用户目录和密钥目录。
3. 环境变量采用白名单，不向目标代码透传 LLM Key、数据库密码、Git Token；输出设置字节上限并保存截断原因。
4. 增加 Workspace/Image 生命周期管理，根据 Python 版本和锁文件生成稳定环境指纹；依赖安装与测试运行分阶段，并明确缓存污染边界。
5. MaintenanceGraph 的 compile、target tests、regression tests 全部通过 ExecutionBackendPort；生产配置缺少 Docker 时早失败，不能静默降级为宿主执行。
6. 支持取消、超时、强制清理和孤儿容器回收。加入读取宿主文件、访问网络、fork bomb、死循环、内存超限、输出洪泛和残留容器安全测试。

第二部分：P5 代码智能与多语言
1. 扩展 Python 分块：模块、类、方法、嵌套函数、装饰器、签名、docstring、父子 Chunk；保留稳定 symbol_id 和源码范围。
2. 定义 LanguageParserPort、SymbolIndexPort、CodeGraphStorePort、CodeGraphRetrieverPort，业务层不得直接依赖 tree-sitter 或 PostgreSQL 表结构。
3. 构建 import、defines、contains、calls、inherits、references 六类边；增量索引时按文件事务化替换节点和边，并处理删除、重命名和 revision 变化。
4. 实现 Parent-Child Retrieval 和 Graph Expansion：先由 BM25/Dense 命中种子符号，再按受限深度扩展调用者、被调用者、父容器和相邻证据，最后进入 RRF；必须有节点数和 Token 上限。
5. 使用 Tree-sitter 实现 Java 第一版：package、class/interface、method、field、import、extends/implements 和方法调用；无法静态解析的动态调用必须标记不确定，不能虚构唯一调用目标。
6. 扩充 retrieval/explain 评测集，至少覆盖 Python 跨文件调用、继承、装饰器以及 Java Controller-Service-Repository 调用链；比较接入图检索前后的真实指标。

第三部分：P6 FastAPI 与领域 API
1. 建立 API/Application/Domain/Infrastructure 清晰边界，FastAPI 路由只做认证、校验和调用 Application Service，不直接操作 Graph、数据库或 Docker。
2. 定义正式数据库实体和 Alembic 迁移：tenants、users、memberships、projects、repository_connections、runs、run_commands、approvals、run_events、artifacts、git_operations、outbox_events；所有业务唯一键包含 tenant_id。
3. 实现统一响应/错误模型、request_id、幂等键、游标分页和 OpenAPI 示例。至少提供认证、项目、索引、Explain/Fix 任务、任务详情、事件、审批、Artifact、Memory、Skill/MCP 管理接口。
4. 实现 SSE。事件先持久化到 run_events，再发布；客户端通过 Last-Event-ID 重连时先补历史再订阅新事件，终态事件必须可重放。
5. API 不得在请求线程运行 Agent、Embedding、Git Clone 或测试；所有长操作只创建命令和 Outbox 事件。

第四部分：Redis Worker、任务一致性与恢复
1. 采用 Redis + Celery 作为第一版任务队列，但通过 TaskDispatcherPort/TaskLeasePort 隔离框架；领域层不得导入 Celery。
2. 使用 PostgreSQL Transactional Outbox 保证“业务状态已提交但任务未投递”可恢复；实现独立 Dispatcher 和幂等消费。
3. Worker 通过稳定 execution_key、数据库租约和心跳获取运行权；重复投递、Worker 崩溃和超时后只能有一个执行者推进同一 Graph。
4. 支持 queued/running/waiting_approval/cancelling/cancelled/completed/failed 状态机。取消操作写入持久化命令，Worker 在节点边界和外部执行期间检查取消信号。
5. 重试只覆盖连接错误、超时和明确可重试的 429/5xx；Schema、权限、审批拒绝和确定性测试失败不得由队列自动盲重试。
6. 增加 API/Worker 分进程、进程重启、重复消息、租约过期、Outbox 补偿、审批恢复和 SSE 重放集成测试。

第五部分：Artifact、租户权限与 Git 平台
1. 定义 ArtifactStorePort，开发实现 LocalFileArtifactStore，生产实现 S3CompatibleArtifactStore；大体积日志、Diff、测试报告和导出结果存 Artifact，数据库只保存摘要、哈希、大小、租户归属和位置。
2. 接入 MinIO 作为本地 S3 兼容环境。下载使用短期签名 URL 或经权限校验的流式接口，禁止暴露任意对象键。
3. 实现 JWT/OIDC 兼容认证边界和 RBAC。角色至少包含 viewer、developer、approver、admin；权限至少覆盖仓库读取、任务执行、代码执行、Patch 审批、Memory 管理、Skill/MCP 管理。
4. 每个数据库查询、向量检索、Artifact、事件流、Checkpoint namespace、Redis key 和 Git 操作都必须携带 tenant_id/project_id；增加横向越权测试。
5. 定义 GitProviderPort。实现 LocalGitProvider 和 GitHubProvider：仓库导入、凭证引用、Clone/Fetch、revision 固定、创建分支、提交和 Pull Request。
6. GitHub Webhook 必须校验签名、防重放并持久化 delivery id；push 事件触发对应项目的增量索引。Token 只通过 SecretProviderPort 获取，不写入 Graph State、Checkpoint、日志或数据库明文字段。
7. 默认保护分支禁止直接写入；批准后的 Patch 先再次校验 revision，再写候选分支。PR 描述包含目标、修改文件、测试、风险、引用和 RepoAgent run id。

第六部分：P7 可观测性、LLM 可靠性与安全治理
1. 实现统一 RunEvent 模型和 EventStorePort，覆盖 run/node/llm/tool/rag/memory/patch/approval/git 全链路事件；事件正文只存摘要和 Artifact 引用。
2. 接入 OpenTelemetry Trace/Metrics 和结构化 JSON 日志，贯通 request_id、tenant_id、project_id、run_id、thread_id、node、attempt；提供 Prometheus endpoint 和本地观测配置。
3. 记录模型供应商、模型名、耗时、Token、估算费用、重试次数；记录工具、RAG、Memory、沙箱和队列耗时。未知值必须为 null，不能填 0。
4. 定义 LLMGatewayPort，在供应商适配器外统一实现超时、有限指数退避、Retry-After、并发限制、熔断和每任务预算；401、Schema 错误和策略拒绝不重试。
5. 增加日志与 Artifact 脱敏、Prompt Injection 边界、仓库数据外发策略、MCP/Skill 供应链校验、审计日志和保留/删除策略。
6. 编写威胁模型，至少覆盖恶意仓库、恶意测试、Prompt Injection、MCP Server、依赖安装、Webhook 重放、租户越权、SSRF、路径穿越、压缩炸弹和 Secret 泄漏，并用自动化测试覆盖可执行控制项。

第七部分：部署、文档和真实端到端验收
1. 提供面向 WSL2 Linux Docker Engine 的 Docker Compose：api、worker、dispatcher、postgres/pgvector、redis、minio，以及必要的迁移/初始化服务；所有服务提供 healthcheck，配置写入 .env.example，任何真实密钥不得提交。不得要求 Windows Container。
2. 提供 dev/test/prod 三类配置说明。生产配置必须要求显式 Secret 和 Docker Sandbox，不能静默使用不安全默认值。
3. 增加一个可提交的 Python 仓库夹具和一个 Java 仓库夹具，完成从注册、索引、Explain、Fix、SSE、审批、沙箱测试到 Git 分支的全链路测试；GitHub PR 测试需要显式凭证，不能用 Mock 冒充真实平台验证。
4. 运行默认离线测试、PostgreSQL/pgvector、Redis、MinIO、Docker 沙箱、API/Worker、多租户和端到端测试；记录真实命令、通过/失败/跳过数、耗时和环境版本。
5. 更新 README、OpenAPI 使用示例、部署文档、ADR、数据模型、状态机、事件协议、威胁模型、故障恢复和备份恢复手册。
6. 把实际完成状态、测试结果、真实性边界、未完成项和下一步回写到本文件“当前执行状态”。不得另建阶段任务文档。

实现约束：
- 所有新增代码注释、报错信息和文档使用中文；类名、字段名及标准协议术语保留英文。
- 先复用现有 Port、Graph、RAG、Memory、MCP、Skill 和 Storage Factory，不做无验收价值的大规模改名或目录重排。
- 不开发前端，不引入 Kubernetes，不接入 Qdrant，不同时实现多个 Git 平台，不把测试或模型执行放进 API 请求线程。
- 不能用 Mock 结果冒充 Docker、PostgreSQL、Redis、MinIO、GitHub 或真实模型集成结果；外部条件缺失时明确标记为部分完成。
- 不编造成功率、性能、Token 或成本；只记录真实评测数据。
- 不自动提交、不自动推送、不创建真实 Pull Request，除非用户在当前任务中另行提供明确授权和测试仓库。
- 工作区已有修改属于用户，不覆盖、不清理无关文件，不使用破坏性 Git 命令。
- 本机 Docker 和所有中间件服务默认位于 WSL2；不得另起一套 Windows 本地中间件，不得把 Docker Desktop 作为唯一前置条件。

WSL2 开发与验收要求：
- 增加 `scripts/wsl/` 下的环境检查、启动、迁移、测试和停止脚本，脚本接收可配置的发行版名称，不能写死用户机器上的发行版。
- 启动前检查 `wsl.exe`、目标发行版、Linux Docker Engine、Compose Plugin、端口占用和数据卷状态，并给出中文错误信息。
- Compose 中 PostgreSQL、Redis、MinIO 只使用容器网络互联；仅把开发调试需要的端口发布到 Windows `localhost`，生产配置不默认暴露数据库端口。
- 提供 Windows PyCharm + WSL Interpreter 的运行说明，以及 Windows Python 连接 WSL 中间件的兼容说明；两种模式共用同一套环境变量命名。
- 为 Windows 路径、`/mnt/<drive>` 路径和容器工作区路径增加单元测试，覆盖空格、中文、大小写盘符和路径越界。
- 状态数据使用命名 Docker Volume；源码、测试产物和数据库数据不能散落在 `/mnt/d` 下。停止服务默认保留 Volume，删除 Volume 必须使用单独的显式命令并提示数据不可恢复。

停止条件：
当第 17.3 节的 10 条后端完成定义全部满足，且所有阶段结果已回写本文档后停止。若真实 GitHub 凭证或 Docker 环境缺失，只能把对应项标记为未完成并给出可复现的剩余命令，不能降低验收标准。
```
