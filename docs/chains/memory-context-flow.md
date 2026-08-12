# Memory 与 Context Builder 链路

## 状态分层

```text
ProjectContext
  ├─ LangGraph State
  │    当前计划、步骤、观察、预算
  │    = Working Memory
  │
  ├─ SQLite Checkpoint
  │    thread 内状态快照和恢复
  │
  ├─ SQLite Memory Store
  │    跨 thread 的经历、事实、感知记录
  │
  └─ Repository RAG
       代码库和文档派生索引
```

## 长期记忆写入

```text
结构化 Workflow Result / 用户 / 系统 / 模型提议
  → MemoryCandidate(candidate_id + memory_key + proposer)
  → MemoryCurator
       ├─ revision / importance / Evidence 硬校验
       ├─ candidate_id 幂等
       ├─ memory_key 精确去重与冲突定位
       ├─ Create / Ignore / Supersede / Reject
       └─ 高风险变更进入 Pending Review
  → MemoryWrite
       ├─ type
       ├─ claim_status
       ├─ importance
       ├─ scope + revision
       ├─ source + source_id
       ├─ evidence
       └─ TTL
  → 事务外 Embedding
  → 数量和维度校验
  → SQLite 事务
       ├─ memories
       ├─ memories_fts
       ├─ memory_index_state
       └─ memory_curation_decisions
```

网络 Embedding 不在 SQLite 写事务内执行，避免远程延迟长期占用数据库写锁。

## 记忆检索

```text
query + ProjectContext
  → project_id 隔离
  → active + expires_at 尚未到期
  → type / claim_status / importance
  → project scope 或 current revision
  → BM25 ranks + Dense ranks
  → RRF + importance
  → MemoryHit(stale_revision, evidence)
```

正常 Agent Tool 固定 `claim_status=verified` 且不包含 stale revision。

## 上下文组装

```text
Gather
  ├─ 宿主系统指令
  ├─ 用户任务
  ├─ Graph Working State
  ├─ Memory hits
  ├─ RAG hits
  └─ Tool observations

Normalize / Structure
  → ContextPacket
  → 来源/信任校验
  → citations / priority / mandatory

Budget / Select
  → 去重
  → mandatory
  → priority
  → token budget

Compress Decision
  → Drop / Compress / Fail
  → Compressor 只能返回正文，安全元数据由宿主继承
  → 压缩后重新计算 token
  → 有限次 Re-budget，仍超限则 Drop

Assemble
  ├─ TRUSTED_INSTRUCTIONS
  ├─ USER_REQUEST
  ├─ TRUSTED_RUNTIME_STATE
  └─ UNTRUSTED_EVIDENCE

Deliver
  → Planner / ReAct / Reflector user message
```

## 记忆更新与遗忘

```text
旧事实 A
  → 使用相同 memory_key 提交新事实 B
  → Curator 决定自动提升或 Pending Review
  → supersede(A, B)
  → 单事务写入 B + A.status=superseded

主动遗忘 / TTL 到期
  → 删除 FTS
  → 擦除 content/evidence/tags/vector
  → 保留 forgotten/expired 墓碑
  → memory_lifecycle_events 记录 actor + reason
```

工作流通过 `MemoryAwareWorkflowRunner` 在运行完成后自动提交情景记忆候选；同一
`run_id` 重放会返回原决策，不会重复生成记忆。

## 三类记忆形成

```text
Episodic 热路径
  Workflow Result
  → 自动形成带 run/thread/revision/Evaluation 的情景候选

Semantic 热路径（配置 StructuredJSONClient 时）
  Workflow Result + Evidence Catalog
  → 结构化 LLM 提取稳定事实草稿
  → hypothesis 可入库；verified 必须 Pending Review

Semantic 慢路径
  按主题召回多条 verified Episodic Memory
  → LLM 归纳重复模式或项目知识
  → 继续经过 Curator，而不是直接升级事实

Perceptual 热路径
  ToolResult.metadata.perceptual_observations
  → Artifact URI + media type + description + evidence
  → Perceptual Candidate
  → 只有宿主信任名单内的感知工具可自动发布 verified
```

语义或感知提取失败只记录 formation error，不会把已经客观完成的主工作流改判失败。
