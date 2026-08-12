# 模块 09 面试讲解：Memory 与 Context Engineering

## 30 秒回答

我把状态分成四层：LangGraph State 是当前 run 的 Working Memory；Checkpoint 保存 thread 快照用于恢复；Memory Store 保存跨 thread 的情景、语义和感知记忆；RAG 保存代码库派生索引。长期记忆带 claim status、Evidence、importance、scope、revision 和 TTL，默认只检索 active verified。Context Builder 再把系统指令、用户任务、Graph State、Memory、RAG 和工具观察转换为 Packet，按信任分区、去重、优先级和 token 预算选择，最终控制 Planner、ReAct 和 Reflector 实际看到的上下文。

## 2 分钟回答

Memory 最容易和 Checkpoint、RAG 混淆。我的划分是：当前计划和工具轨迹已经在 LangGraph State 中，它们属于线程内 Working Memory；Checkpoint 是这个状态的持久化快照，用于故障恢复；长期 Memory 才保存跨任务复用的经历和知识；RAG 检索代码库原文。

长期 Memory 使用 SQLite 保存 episodic、semantic 和 perceptual 三类记录。写入不是把一段文本直接插入数据库，而是先形成带 candidate id、稳定事实键和 proposer 的 MemoryCandidate，再由 Curator 做硬校验、幂等去重、冲突识别、TTL、自动替代或人工审核。正常 ReAct 只开放只读搜索；工作流完成后自动提交可追溯的情景候选，模型提出的 verified 事实必须等待审核。

形成机制也按类型分开：Episodic 从结构化 Run Result 自动形成；Semantic 在热路径中由结构化 LLM 从一次运行提取候选，也可以在慢路径中从多次 verified Episodic 归纳知识；Perceptual 从感知工具发布的 Artifact 观察形成。三种来源最后都进入同一个 Curator，不允许抽取模型绕过审核。

版本级记忆默认只对同一 repo revision 有效，项目级记忆适合稳定架构规则。相似度检索前先按项目、状态、类型、可信度、重要性和 revision 过滤，再融合 BM25、Dense rank 和 importance。Embedding 模型变化要显式 reembed。

检索结果不等于模型上下文。Planner 按总目标、每个 ReAct Step 按步骤目标自动预检索 RAG 与 verified Memory，结果先转换成 ContextPacket。Structure 后预留输出 Token 并检查输入预算：mandatory 超限直接失败；高价值 Evidence 通过 Compressor 压缩并重新计数；低价值或多次压缩仍超限的 Packet 被排除。压缩器只能返回正文，source、trust、citations 和 priority 由宿主继承。最后再组装 Trusted Instructions、User Request、Trusted Runtime State 和 Untrusted Evidence。

## 面试官可能追问

### Checkpoint 和 Memory 有什么区别？

Checkpoint 面向同一 thread 的执行恢复，保存 Graph State 快照；Memory 面向跨 thread 的信息复用，保存筛选后的经历和事实。Checkpoint 回答“任务执行到哪里”，Memory 回答“以前发生过什么、已经确认了什么”。

### Memory 和 RAG 有什么区别？

Memory 的来源是用户交互、Agent 经历和历史任务，有形成、整合、替代、遗忘等生命周期；RAG 的来源是代码、文档和外部知识库，有导入、分块、更新和删除生命周期。二者可以复用向量技术，但不能共用可信度和版本语义。

### 为什么不把 Working Memory 也写进 Memory Store？

当前计划、计数器和中间观察已经在 LangGraph State，并由 Checkpoint 恢复。复制到长期 Store 会产生双写一致性、重复检索和过期状态问题。真正值得跨任务复用的内容在任务结束后再形成长期记忆。

### 情景记忆和语义记忆怎么区分？

情景记忆记录某次具体任务发生了什么，带 run、时间和 revision；语义记忆保存从证据确认的稳定事实或规则，不强调某次事件。项目既能在单次任务结束后提取语义候选，也能按主题召回多次 verified 情景，在慢路径中归纳重复模式；归纳结果仍需 Curator 和审核。

### Semantic Memory 到底怎样生成？

有三条入口。用户或宿主可以显式提交有证据的事实；配置结构化模型客户端后，工作流结束钩子会把 Run Result、步骤观察和允许引用的 Evidence Catalog 交给语义提取器；后台还可以按主题召回多条 verified Episodic 做慢路径归纳。模型输出只是 Draft，hypothesis 可以按策略保存，verified 必须审核，模型还不能引用 Evidence Catalog 之外的证据。

### Perceptual Memory 到底怎样生成？

截图、日志或音频首先由感知工具或多模态模型观察，工具在统一 metadata 中发布 Artifact URI、media type、description、Evidence 和可信状态。工作流结束后感知提取器把它转换为 Perceptual Candidate。Memory 层不负责自己理解所有二进制，而是管理结构化观察的可信度和生命周期；只有宿主信任名单内的工具可以自动发布 verified，远程或未知工具会降级为 hypothesis，模型生成的 verified 观察进入审核。

### 为什么模型不能直接写 verified Memory？

模型可能把推断和幻觉写成事实，之后重复召回会形成错误反馈循环。当前模型只能搜索；模型可以提出 Candidate，但 verified 候选会进入持久化审核队列，只有宿主或用户批准后才能创建或替代活动事实。模型不掌握最终写权限。

### 记忆是什么时候写入，由谁判断？

有三类触发：工作流结束后由组合入口自动提交情景候选；用户或宿主可以显式提交项目事实和假设；未来模型提议也只能进入同一个候选入口。Curator 先检查 Evidence、版本、重要性、候选幂等和事实键，再决定创建、忽略、替代、等待审核或拒绝。因此“是否值得长期保存”由宿主定义的确定性 Policy 决定，模型只负责提议，用户负责高风险事实确认。

### 更新时怎样知道应该更新哪一条？

不是拿语义搜索第一名直接覆盖，而是使用业务稳定的 `memory_key` 表示事实槽位，例如 `config:retry-limit`。同一项目同一事实键最多有一条 active 记忆；新候选先与它比较。假设被客观证据确认可以自动提升，同一 verified 事实内容变化默认等待审核，批准后原子 supersede。这样把“相关”与“同一事实”分开。

### 删除什么时候发生，由谁决定？

主动遗忘由用户或宿主指定 memory id、actor 和 reason；时间性记忆由 Curator 设置 TTL，达到 expires_at 后立刻从检索候选中排除，维护任务再擦除正文、Evidence、向量和 FTS。两者都保留不可召回的状态墓碑和生命周期审计事件。模型没有 forget 权限。

### claim_status 有什么用？

它把“模型猜测”“证据确认”和“已经证伪”分开。默认 Context 只召回 verified；调试时可显式读取 hypothesis/refuted。相似度高不代表事实为真。

### project scope 和 revision scope 怎么选？

“项目统一使用 Python 3.12”可能是项目级规则；“当前 commit 的 BillingService 位于某文件”是 revision 级事实。版本变化后，后者默认不再进入 Context。

### 为什么记忆需要遗忘？

永久保存会增加隐私风险、存储成本、冲突和检索噪声。TTL 用于临时事件，forget 用于主动擦除，supersede 用于事实更新。当前实现会删除正文、向量和 FTS，只保留最小状态墓碑。

### Memory 检索为什么先做元数据过滤？

向量相似只能说明语义接近，不能判断项目、版本、可信状态和是否过期。先过滤可以防止其他项目或已证伪记忆进入候选池，也减少向量计算量。

### Context Engineering 和 Prompt Engineering 有什么区别？

Prompt Engineering 主要设计指令表达；Context Engineering 管理一次模型调用看到的全部内容，包括系统指令、用户请求、State、Memory、RAG、工具、输出格式和预算。Prompt 是 Context 的一部分。

### 为什么检索结果不能全部放进 Prompt？

检索阶段追求召回，允许较大的候选池；Context 阶段受 token、注意力、延迟和输出空间限制，需要选择。更多内容可能带来重复、过期和相互冲突的信息。

### mandatory 为什么超限直接失败？

系统约束、用户任务和关键运行态如果被静默截断，模型可能在缺少权限或目标条件下行动。显式失败能让上层先摘要状态或扩大预算，而不是生成不可解释结果。

### 为什么要给输出预留 token？

模型上下文窗口通常包含输入和输出总量。输入塞满窗口会导致输出截断或请求失败，所以预算先扣除 `reserved_output_tokens`。

### token 估算准确吗？

当前是保守近似：中文字符约一个 token，其他非空字符约四字符一个 token。它适合离线和供应商无关测试，但接入固定生产模型时应换成对应 tokenizer，并保留安全余量。

### 信任分区能彻底解决 Prompt injection 吗？

不能。分区和标签帮助模型区分指令与数据，正文标签还会被转义；真正安全仍依赖模型看不到未授权工具、Registry 校验、路径边界、执行授权和 Evaluator。Prompt injection 必须纵深防御。

### 为什么不能直接按字符截断 Packet？

自由截断可能删除否定词、数字和 Evidence 来源。当前实现只对高价值 Evidence 使用显式 Compressor，产物标注省略范围并继承引用，同时记录替代 Packet、策略和压缩前后 Token；指令和用户请求不做有损压缩。更成熟的语义 Compressor 还需要单独的压缩质量评测。

### Context Engineering 为什么需要压缩决策？

Structure 之后才能知道每个 Packet 的来源、信任、优先级和引用，也才能可靠估算 Token。超预算时根据 mandatory 和 priority 选择 Fail、Compress 或 Drop；压缩后重新渲染四个信任分区并计数，最多有限次。Compressor Port 没有返回 Packet 的权限，只返回正文，所以 trust 和 citations 必须由宿主继承，无法在压缩阶段提升权限。

### Context Builder 是否真的接入了 Agent？

是。StructuredPlanner、StructuredDecisionClient 和 StructuredReflector 都用它构造 user message。工具定义和预算进入 Runtime State，工具结果进入 Untrusted Evidence，JSON Schema 仍通过 system message约束。

## 当前代码证据

- `memory/models.py`：记忆类型、可信状态、范围和生命周期模型。
- `memory/curation.py`：候选幂等、去重、冲突、TTL 与人工审核策略。
- `memory/formation.py`：语义提取、情景归纳和感知制品形成链路。
- `memory/store.py`：SQLite、FTS、向量检索、替代、遗忘、TTL 和重建。
- `memory/manager.py`：自动工作流触发和受控生命周期入口。
- `memory/tool.py`：只读 verified Memory Tool。
- `context_engineering/models.py`：ContextPacket 和选择审计。
- `context_engineering/builder.py`：token 预算、去重、优先级和信任分区。
- `context_engineering/compression.py`：Compressor Port、确定性压缩和安全元数据边界。
- `llm/adapters.py`：Planner、ReAct、Reflector 的实际接入。
- `tests/test_memory_and_context.py`：19 个 Memory/Context 基础测试。
- `tests/test_memory_curation.py`：14 个完整治理与形成链路测试。

全项目当前共 214 个测试：211 个通过，3 个外部集成测试默认跳过。

## 主动说明的局限

1. 冲突定位依赖稳定 memory_key，尚未做实体抽取和语义槽位合并。
2. 没有用户级和组织级 Memory namespace。
3. token 计数不是供应商精确 tokenizer。
4. 默认是确定性抽取压缩，尚未实现语义压缩器和独立压缩质量评测集。
5. SQLite Store 不适合高并发生产场景。
6. Perceptual Memory 管理结构化 Artifact 观察，尚未内置具体视觉模型和多模态 Embedding。
