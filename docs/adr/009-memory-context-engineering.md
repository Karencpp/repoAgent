# ADR-009：长期记忆生命周期与预算化上下文工程

- 状态：Accepted
- 日期：2026-07-31
- 模块：Memory and Context Engineering

## 背景

RepoAgent 已经有 LangGraph State、SQLite Checkpoint 和代码库 RAG，但它们不能替代长期记忆：

- State 保存当前 run 正在发生什么；
- Checkpoint 保存某个 thread 的状态快照，以便恢复；
- RAG 检索目标仓库中的代码和文档；
- Memory 保存跨 run 可复用的经历、事实和感知记录。

如果把所有历史状态、检索结果和工具输出直接拼进 Prompt，会出现上下文膨胀、重复、过期事实、Prompt injection 和输出空间不足。需要一个显式 Context Builder 决定单次调用到底能看到什么。

## 决策

### 1. Graph State 继续承担 Working Memory

当前计划、步骤索引、工具观察、反思次数和预算都已经存在 LangGraph State，并由 Checkpoint 按 thread 持久化。它们属于当前任务的动态短期状态，不再复制进长期 Memory Store。

这与 LangGraph 官方概念一致：短期记忆是线程范围内的 State，Checkpoint 让线程可恢复；Store 用于跨线程长期记忆。

### 2. 长期 Store 保存三类记忆

```text
episodic：某次任务发生过什么
semantic：跨任务可复用的事实、规则或决策
perceptual：截图、日志、音频等感知产物的文字描述和引用
```

Perceptual Memory 当前只保存文字描述与 artifact Evidence，不保存或向量化原始二进制。Working Memory 不进入这个 Store。

### 3. 每条记忆必须带可信状态和来源

MemoryRecord 不只有 content，还包括：

- project_id；
- memory_type；
- `claim_status=hypothesis|verified|refuted`；
- importance；
- `scope=project|revision`；
- repo_revision；
- source 和 source_id；
- evidence 与 tags；
- TTL；
- active、superseded、forgotten、expired 状态；
- Embedding 模型和维度。

`verified` 记忆必须有 Evidence。模型提出的可能原因只能写成 hypothesis，不能因为多次出现就自动升级为事实。

### 4. 正常模型路径只开放只读 Memory Tool

`search_project_memory` 只返回当前项目的 active、verified 记录，默认排除其他 repo revision 的版本级事实。

模型不能直接调用 `write_verified_memory`。`MemoryAwareWorkflowRunner` 在工作流完成后，由 MemoryManager 从结构化 run id、状态、停止原因和 Evaluation Evidence 自动形成情景记忆候选；同一 run id 使用稳定 candidate id 和 memory key，重放不会重复写入。

所有写入先经过 MemoryCurator。候选包含 `candidate_id`、稳定 `memory_key`、proposer、rationale 和 MemoryWrite。Curator 用确定性规则完成硬校验、幂等、精确去重、冲突定位和 TTL 设置，再产生 created、ignored_duplicate、superseded、pending_review 或 rejected 决策。模型提出 verified 事实，以及同一已验证事实键上的内容变化，必须进入持久化人工审核队列。

三类记忆使用不同的形成策略：

- Episodic：每次工作流结束后从结构化 Run Result 自动形成；
- Semantic 热路径：配置 StructuredJSONClient 后，从本次 Run、步骤观察和 Evidence Catalog 中抽取跨任务事实候选；
- Semantic 慢路径：按主题召回多条 verified Episodic，再归纳重复模式或项目知识；
- Perceptual：感知工具通过 `ToolResult.metadata.perceptual_observations` 发布 Artifact URI、媒体类型、描述和 Evidence，由工作流结束钩子自动形成候选。

LLM 提取的 verified Semantic/Perceptual 仍然进入审核。外部工具元数据不天然可信；只有宿主显式加入 `trusted_perception_tools` 的工具才能自动发布 verified 感知观察，其他工具的声明会降级为 hypothesis。形成失败与主任务结果隔离，只记录 formation error。

### 5. 使用替代、遗忘和 TTL 管理生命周期

- `supersede`：原子写入新事实，再把旧事实标记为 superseded；
- `forget`：擦除正文、Evidence、tags、向量和 FTS 条目，只保留最小墓碑；
- `expire`：对达到 TTL 的 active 记忆执行相同的数据擦除；
- `reembed_project`：Embedding 模型变化时显式重建活动记忆。

遗忘要求显式 actor 和 reason；TTL 由确定性保留策略决定。两种擦除都写入 `memory_lifecycle_events`。检索本身也比较 `expires_at`，所以即使清理任务尚未运行，过期内容也不会被召回；维护任务负责进一步擦除正文、向量和 FTS。

“什么都永久保存”会增加冲突、隐私风险和检索噪声，所以遗忘是记忆系统的正常能力。

### 6. 记忆检索先过滤，再融合排名

检索顺序：

```text
project_id + active
  → memory type
  → claim status
  → importance
  → revision scope
  → BM25 + Dense Retrieval
  → RRF + importance
```

元数据过滤必须先于语义相似度，避免其他项目、旧 revision 或 refuted 记录进入候选池。默认只召回 verified；调试时可以显式查询 hypothesis、refuted 或 stale revision。

### 7. ContextPacket 是所有上下文来源的统一单位

每个 Packet 保存：

- packet_id；
- source；
- trust；
- content；
- priority；
- mandatory；
- citations；
- dedupe_key；
- created_at。

RAG 命中、Memory 命中和工具观察都先转换为 Packet，再交给 Context Builder。这样“检索到了什么”和“最终给模型看了什么”可以分别审计。

### 8. 使用四个信任分区

```text
TRUSTED_INSTRUCTIONS
  宿主系统指令、未来经过校验的 Skill

USER_REQUEST
  用户任务，不自动提升为系统策略

TRUSTED_RUNTIME_STATE
  Graph 状态、工具白名单、预算和客观控制字段

UNTRUSTED_EVIDENCE
  Memory、RAG、源码、日志和工具输出
```

ContextPacket 校验来源和 trust 的合法组合。RAG 内容不能声明自己是 trusted instruction。Packet 使用 JSON 承载，正文中的 `<`、`>` 会转义，降低伪造分区标签的风险。

Prompt 标记只能帮助模型理解，真正安全边界仍是 Tool Registry、路径沙箱、授权和 Evaluator。

### 9. 给输出预留 token，强制上下文绝不静默截断

ContextBuilder 配置：

```text
model_context_window
- reserved_output_tokens
= input_budget_tokens
```

选择顺序：

1. 按 dedupe_key 或内容哈希去重；
2. 保留系统、任务和工作状态等 mandatory Packet；
3. 可选 Packet 按 priority、来源和时间排序；
4. 在预算内逐个加入；
5. 高价值 Evidence 超预算时进入 Compressor Port；
6. 每次压缩后重新渲染完整分区并重新计算 token，最多执行有限次；
7. 记录 included、duplicate、compressed 或 budget_exceeded，并保存替代 Packet、策略、压缩前后 token 和引用。

mandatory 本身超限时显式失败，不静默删除系统约束。低价值或无法安全压缩的可选 Packet 会被排除，并继续尝试后续内容。

当前实现提供 `ContextCompressor` Port，默认使用确定性的首尾抽取压缩，并明确标注省略字符数。压缩器只能返回正文和策略，新的 Packet 由宿主继承原始 source、trust、citations、priority 和 mandatory，因此不能借压缩升级信任。高价值 Evidence 经过 Compress 与 Re-budget 后仍放不下就排除；系统指令、用户请求和可信运行态不会交给默认压缩器改写。

### 10. 第一版使用保守 token 估算端口

`TokenCounter` 是可替换端口。默认 HeuristicTokenCounter 将中文字符近似按一个 token，其他非空字符约四字符一个 token。

它不是供应商 tokenizer 的精确结果，因此预算应保留安全余量。接入具体生产模型时，应使用对应 tokenizer 或供应商计数接口。

### 11. Planner、ReAct 和 Reflector 已接入预检索与 Context Builder

- 用户目标进入 USER_REQUEST；
- 工具定义、剩余预算和当前计划进入 TRUSTED_RUNTIME_STATE；
- 工具观察和步骤结果进入 UNTRUSTED_EVIDENCE；
- Planner 按总目标、每个 ReAct Step 按步骤目标自动预检索 RAG 和 verified Memory，重复轮次使用查询缓存；
- JSON Schema 和角色约束仍使用真实 system message。

因此 Context Builder 不是孤立工具，而是已经控制真实 GLM 适配器的用户消息内容。

## 没有选择的方案

### 把全部 Checkpoint 历史当作长期记忆

Checkpoint 面向线程恢复，包含大量执行细节；跨线程直接搜索会带来重复、内部状态泄漏和版本漂移。

### 每轮结束都让模型自由总结并写入 verified Memory

模型总结可能把假设和幻觉固化成长期事实，后续召回会不断强化错误。

### 把所有历史消息继续追加到 Prompt

会增加 token、延迟和注意力噪声，旧信息还可能覆盖当前事实。

### 只按相似度检索记忆

相似但已 refuted、过期或属于其他 revision 的记忆不能进入当前任务。元数据过滤必须优先。

### 自动截断任意 Packet

截断可能删除 Evidence 来源、否定词和关键数字。第一版选择完整加入或完整排除，并记录原因；后续摘要必须单独保存来源和压缩记录。

## 当前局限

- 没有用户级与组织级 namespace，当前以 project_id 为主。
- 当前冲突定位依赖调用方提供稳定 memory_key，尚未做实体抽取或语义槽位归并。
- Curator 采用确定性规则，尚未接入敏感信息分类器和容量淘汰策略。
- 默认 token 计数是估算值。
- Context Builder 尚未做摘要、分层压缩和信息损失评测。
- SQLite Store 不面向高并发生产部署。
- Perceptual Memory 只保存文字描述和 artifact 引用。
- 原始图片、音频等二进制的理解由外部感知工具或多模态模型负责，Memory 层消费其结构化观察，不内置具体视觉模型。

## 官方依据

- [LangGraph Memory](https://docs.langchain.com/oss/python/langgraph/add-memory)
- [LangChain Memory 概念](https://docs.langchain.com/oss/python/concepts/memory)
- [LangChain Context Engineering](https://docs.langchain.com/oss/python/langchain/context-engineering)
- [LangGraph Persistence](https://docs.langchain.com/oss/python/langgraph/persistence)

## 验证证据

Memory 治理与形成测试共 14 个，覆盖幂等写入、人工审核、事实替代、假设提升、审核竞争冲突、TTL、遗忘审计、语义热路径提取、多情景归纳、感知制品形成、可信工具降级和形成失败隔离。全项目当前共 214 个测试：211 个通过，3 个外部集成测试默认跳过。
