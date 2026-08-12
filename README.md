# RepoAgent

RepoAgent 是一个面向 Python 代码库的可解释维护 Agent。

## 现在可以真实运行的两条链路

RepoAgent 不会默认操作启动目录。无论解释还是维护，都必须传入目标仓库路径，
或者先注册项目再通过项目名选择。RAG、Memory 和 Checkpoint 数据保存在独立状态目录，
不会混入目标代码库。

先安装当前项目：

```powershell
python -m pip install -e .
```

配置已经轮换过的 GLM 密钥：

```powershell
$env:ZHIPUAI_API_KEY="你的新密钥"
```

也可以切换到 DeepSeek。推理供应商只影响 Planner、ReAct、Reflector 和语义记忆，
不会改变 LangGraph、工具、RAG、Memory 或候选验证协议：

```powershell
$env:DEEPSEEK_API_KEY="你的 DeepSeek 密钥"
repo-agent explain --llm-provider deepseek --repo "D:\code\target-project" "解释核心调用链"
```

如果希望从 PyCharm Run Console 动态输入问题，可以省略最后的问题，或使用连续交互模式：

```powershell
repo-agent --state-dir ".\output\my-state" chat --llm-provider deepseek --repo "D:\code\target-project"
```

`chat` 每次读取一个问题并形成独立可恢复任务；输入 `退出`、`exit` 或 `quit` 结束。
运行过程中会实时输出索引、规划、ReAct 轮次、工具调用、评估和 Memory 治理事件，
不会在结构化模型调用期间长时间保持空白。

只读解释任意 Python 仓库：

```powershell
repo-agent explain --repo "D:\code\target-project" "这个项目的入口和核心调用链是什么？"
```

解释链路结束后会再经过 Final Answer 合成层。该层只消费已经通过 Evaluator 的步骤结果和
Evidence，并用 `read_file_range` 重新复核 `path:start-end` 引用；无法复核的重要断言会在
答案边界中标记为 unsupported，不会反向改写工作流的客观状态。

默认检索使用不联网的特征哈希向量，便于本地运行但不等同于语义 Embedding。
如果代码允许发送到外部服务，可以显式开启 GLM Embedding：

```powershell
$env:ALLOW_EXTERNAL_CODE_EMBEDDING="true"
repo-agent explain --repo "D:\code\target-project" --use-glm-embedding "解释核心领域模型"
```

可以显式装配本地或 HTTP MCP Server。只有配置文件中经过宿主审核的工具策略会注册到
Tool Registry，远程描述和 annotations 不会自动成为授权：

```powershell
repo-agent explain --repo "D:\code\target-project" --mcp-config ".\configs\mcp.local-registry.example.json" "列出入口文件"
```

长期记忆慢路径归纳需要显式 CLI 触发，并继续经过 Curator 治理：

```powershell
repo-agent memory consolidate --repo "D:\code\target-project" --topic "测试失败模式"
```

也可以注册后按名称选择：

```powershell
repo-agent project add --repo "D:\code\target-project" --name target-project
repo-agent explain --project target-project "请求进入系统后经过哪些模块？"
```

如果运行在 checkpoint 边界中断，可以在仓库 revision 未变化时恢复同一线程：

```powershell
repo-agent resume --project target-project --thread-id run-20260802T120000Z-abcd1234
```

生成维护候选时，真实仓库不会被修改。`--allow-code-execution` 是对隔离副本运行
pytest 的显式授权；未提供时验证会停在权限边界：

```powershell
repo-agent fix --repo "D:\code\target-project" "修复 add 函数的计算错误" --allow-code-execution
```

命令会输出 unified diff、四阶段验证报告和 `proposal_id`。人工确认后，使用单独命令
显式批准回写：

```powershell
repo-agent apply --proposal-id proposal-xxxxxxxxxxxxxxxxxxxx --approve
```

Current maintenance workflow commands:

```powershell
repo-agent fix --repo "D:\code\target-project" "fix objective" --allow-code-execution
repo-agent resume-fix --repo "D:\code\target-project" --thread-id fix-thread --approve
repo-agent resume-fix --repo "D:\code\target-project" --thread-id fix-thread --reject
repo-agent apply --proposal-id proposal-xxxxxxxxxxxxxxxxxxxx --approve
```

Offline eval commands:

```powershell
repo-agent eval retrieval --dataset evals/retrieval/python-small.jsonl
repo-agent eval explain --dataset evals/explain/python-small.jsonl
repo-agent eval patch --dataset evals/patch/python-small.jsonl
```

## PostgreSQL/pgvector 可选后端

SQLite 仍是默认开发后端。需要生产级持久化时，先启动本地 PostgreSQL/pgvector 并执行迁移：

```powershell
docker compose -f docker-compose.postgres.yml up -d
$env:REPO_AGENT_POSTGRES_DSN="postgresql://repo_agent:repo_agent_dev_password@localhost:54329/repo_agent"
python -m pip install -e ".[postgres]"
alembic upgrade head
repo-agent explain --storage-backend postgres --repo "D:\code\target-project" "解释入口"
```

SQLite 状态迁移支持 dry-run 和 execute。Checkpoint 不做猜测转换；旧线程应在 SQLite 完成，
或在 PostgreSQL 后端重新开始：

```powershell
repo-agent migrate-state --sqlite-state-dir ".\output\my-state" --postgres-dsn $env:REPO_AGENT_POSTGRES_DSN --dry-run
repo-agent migrate-state --sqlite-state-dir ".\output\my-state" --postgres-dsn $env:REPO_AGENT_POSTGRES_DSN --execute
```

维护链路的关键不变量：

- 模型只产生结构化修改草稿，旧文件 SHA-256 由宿主读取真实基线后填写。
- 补丁先应用到独立候选工作副本，依次检查变更范围、Python 编译、目标测试和回归测试。
- pytest 优先使用目标仓库的 `.venv`、`venv` 或 `env`，找不到时才明确降级到宿主解释器。
- 未通过全部客观验证、未显式批准或源仓库 revision/文件哈希变化，都会拒绝回写。
- 回写使用同目录原子替换；多文件写入中途失败时会尝试恢复已经修改的文件。

当前已完成：

- 模块 1：显式目标仓库选择、项目注册与多代码库隔离。
- 模块 2：受限仓库工具、统一结果契约与 pytest 执行边界。
- 模块 3：Tool Registry、结构化模型决策与最小 ReAct 控制循环。
- 模块 4：LangGraph 显式状态、Plan/Execute/Evaluate/Reflect/Replan 主闭环。
- 模块 5：SQLite Checkpoint、跨实例恢复、项目线程隔离与幂等键。
- 模块 6：隔离候选工作副本、受控补丁与客观验证闭环。
- 模块 7：真实 GLM HTTP 适配、结构化 Planner/ReAct/Reflector 与密钥隔离。
- 模块 8：结构感知代码分块、增量索引、混合检索与检索质量评测。
- 模块 9：Episodic 自动留档、Semantic 提取/归纳、Perceptual 制品观察、Candidate/Curator 治理、生命周期审计、任务前 RAG/Memory 预检索、可追溯压缩与 Re-budget 上下文工程。
- 模块 10：Agent Skill v2 能力包，包含渐进加载、确定性路由、参考资料、受控脚本、双向 Schema、自测数据、版本恢复与工具权限交集。
- 模块 11：现代 MCP 能力发现、宿主策略映射、HTTP 调用与能力漂移校验。

核心约束：

- RepoAgent 自身源码仓库与目标代码库是两个概念。
- 每次运行必须显式提供 `repo` 路径或已注册的 `project`，不使用当前工作目录兜底。
- Memory、RAG、Checkpoint 和工具沙箱后续都以稳定的 `project_id` 隔离。
- 每次运行重新读取目标仓库 revision，区分项目身份与代码版本。
- Skill 只从显式可信根目录发现；目标代码库中的 `SKILL.md` 和脚本不自动提升为指令或可执行能力。
- Skill 脚本被注册成受作用域保护的普通 Tool，通过 JSON stdin/stdout、输入输出 Schema、超时、输出上限和最小子进程环境执行。
- MCP Server 的工具声明不等于宿主授权，只有本地 Policy 审核项才进入 Tool Registry。
- PostgreSQL/pgvector 是可选后端；缺少 DSN、迁移或可选依赖会早失败，默认离线测试不依赖 Docker。

运行全部测试：

```powershell
python -m unittest discover -s tests -v
```

模块一知识材料：

- `docs/adr/001-explicit-project-context.md`
- `docs/chains/project-selection-flow.md`
- `docs/failures/cross-project-contamination.md`
- `docs/failures/test-temp-directory-permission.md`
- `docs/interview/module-01-project-context.md`

模块二知识材料：

- `docs/adr/002-restricted-repository-tools.md`
- `docs/chains/tool-call-flow.md`
- `docs/failures/test-failure-vs-tool-failure.md`
- `docs/failures/path-sandbox-is-not-process-sandbox.md`
- `docs/interview/module-02-repository-tools.md`

模块三知识材料：

- `docs/adr/003-structured-react-runtime.md`
- `docs/chains/react-control-loop.md`
- `docs/failures/invalid-model-output.md`
- `docs/failures/duplicate-tool-loop.md`
- `docs/interview/module-03-tool-registry-react.md`

模块四知识材料：

- `docs/adr/004-langgraph-explicit-workflow.md`
- `docs/chains/langgraph-main-workflow.md`
- `docs/failures/reflection-without-objective-feedback.md`
- `docs/failures/overwriting-plan-history.md`
- `docs/interview/module-04-langgraph-workflow.md`

模块五知识材料：

- `docs/adr/005-sqlite-checkpoint-recovery.md`
- `docs/chains/checkpoint-recovery-flow.md`
- `docs/failures/checkpoint-is-not-exactly-once.md`
- `docs/failures/checkpoint-serialization-type-drift.md`
- `docs/interview/module-05-checkpoint-recovery.md`

模块六知识材料：

- `docs/adr/006-isolated-candidate-evaluation.md`
- `docs/chains/candidate-validation-flow.md`
- `docs/failures/workspace-copy-is-not-process-sandbox.md`
- `docs/failures/stale-patch-overwrites-new-code.md`
- `docs/interview/module-06-candidate-evaluation.md`

模块七知识材料：

- `docs/adr/007-structured-glm-adapter.md`
- `docs/chains/structured-llm-flow.md`
- `docs/failures/json-mode-is-not-authorization.md`
- `docs/interview/module-07-structured-glm-adapter.md`

模块八知识材料：

- `docs/adr/008-repository-rag.md`
- `docs/chains/repository-rag-flow.md`
- `docs/failures/stale-rag-index.md`
- `docs/failures/embedding-space-mismatch.md`
- `docs/interview/module-08-repository-rag.md`

模块九知识材料：

- `docs/adr/009-memory-context-engineering.md`
- `docs/chains/memory-context-flow.md`
- `docs/failures/model-hypothesis-becomes-memory.md`
- `docs/failures/context-stuffing.md`
- `docs/interview/module-09-memory-context-engineering.md`

模块十知识材料：

- `docs/adr/010-progressive-skill-system.md`
- `docs/chains/skill-activation-flow.md`
- `docs/failures/skill-is-not-permission.md`
- `docs/failures/eager-loading-all-skills.md`
- `docs/interview/module-10-agent-skills.md`

模块十一知识材料：

- `docs/adr/011-modern-mcp-gateway.md`
- `docs/chains/mcp-tool-flow.md`
- `docs/failures/remote-mcp-metadata-is-not-policy.md`
- `docs/failures/mcp-tool-error-vs-protocol-error.md`
- `docs/interview/module-11-mcp-gateway.md`
