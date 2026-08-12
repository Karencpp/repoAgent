# 模块 08 面试讲解：代码库混合 RAG

## 30 秒回答

我给 RepoAgent 增加了版本绑定的代码库 RAG。Python 按 AST 顶层符号切块，Markdown 按标题层级切块，每个 chunk 保留路径、行号和哈希。文件使用 SHA-256 增量索引；关键词侧用 SQLite FTS5 BM25，语义侧用 Embedding 和余弦相似度，再用 RRF 融合。索引按 project_id 隔离并绑定 repo revision 和 Embedding 模型空间，过期就拒绝检索。RAG 只负责召回候选，Agent 仍要用 read_file_range、AST 和测试验证事实。

## 2 分钟回答

代码库维护既需要精确查找，也需要概念性导航。只用 grep 时，用户说“恢复登录凭证”，代码里可能叫 `reset_password`，表达不一致就会漏掉；只用向量检索又可能对精确类名、错误码和配置项不够稳定，所以我做了混合检索。

建库阶段从显式 ProjectContext 出发，只扫描白名单文本，跳过缓存、符号链接和大文件。Python 通过 AST 按顶层函数和类分块，同时保留导入、常量等模块级片段；Markdown 保留 heading path。每个 chunk 都有 path、line range 和内容 hash。

索引不是每次全量重建。文件 SHA-256 不变就复用原向量，变化或新增才重新分块，删除文件会同步清理 FTS 和向量。Embedding 完成后通过一个 SQLite 事务更新，避免留下半索引。

查询时 BM25 和 Dense Retrieval 各召回一个扩大后的候选池，然后用 RRF 按排名融合。这样不需要直接比较量纲不同的 BM25 原始分数和余弦分数。结果必须携带引用，随后由精确仓库工具确认。

可靠性方面，索引绑定 project_id、repo revision、embedding model 和 dimensions。旧 revision、不同模型或不同维度都不能继续查。GLM Embedding 还要求显式外部数据授权，因为有 Key 不代表允许把私有代码发送到外部。

最后我用带 relevant paths 的查询集计算 Recall@K 和 MRR，把“检索没找对”和“模型拿到正确资料但回答错”分开评估。

## 面试官可能追问

### 已经有 grep 和 AST，为什么还需要 RAG？

grep 和 AST 适合已知标识符或文件的精确验证；RAG 适合不知道代码术语和位置时做候选召回。链路应该是 RAG 缩小范围，再用精确工具验证，而不是二选一。

### RAG 和 Memory 有什么区别？

本模块 RAG 的数据来自目标代码和文档，回答“仓库里写了什么”；Memory 主要来自 Agent 的历史任务、用户偏好和经验，回答“以前发生过什么”。底层可以复用 Embedding 和检索，但生命周期和可信度不同。

### 为什么 Python 要按 AST 分块？

函数和类是自然语义单元，能保留签名、docstring 和实现之间的关系，也能给出稳定符号名和行号。固定字符切分可能把签名和函数体分开。但 AST 不是绝对方案：语法损坏时要退回文本分块，超大类仍要二次切分。

### Chunk 越大越好吗？

太大容易混入多个主题、浪费上下文；太小会丢失语义和调用关系。当前按结构边界优先，再用字符预算限制，并保留少量行 overlap。最优值需要根据真实查询集评测，不能只凭经验固定。

### 为什么使用混合检索？

BM25 擅长精确标识符、错误码和罕见术语；Dense 擅长同义表达和跨语言语义。代码检索同时存在两类查询，混合通常比单一路径更稳。

### 为什么使用 RRF，而不是直接把两个分数相加？

BM25 分数和余弦相似度的量纲、范围和分布不同，直接相加需要校准。RRF 只依赖每一路的 rank，容易解释，对不同后端更稳定。缺点是会丢失原始分数差距，生产系统可以通过学习排序或 Reranker 改进。

### 为什么选 SQLite，不直接选 Qdrant？

这个项目不面向上线，SQLite 零部署、可事务化、FTS5 自带 BM25，能够清楚展示索引原理。当前向量是 O(N) 精确扫描，超过配置上限会报错。需要几十万以上 chunk、ANN、过滤、并发和分布式部署时，再换 Qdrant 或 pgvector。

### SQLite 里怎么保存向量，有什么问题？

当前以 JSON 数组保存，查询时反序列化并计算余弦，优点是简单透明；缺点是空间效率和查询速度差，也没有向量索引。这是刻意的学习实现，不是对生产向量数据库的替代。

### 增量索引怎么保证一致性？

先扫描并计算文件哈希，在事务外完成所有新 Embedding 和维度校验；之后用一个 SQLite 事务删除旧条目、插入新 chunk、更新文档表和 index state。建库前后还检查 revision，代码中途变化则整次不写入。

### 为什么 Embedding 模型变化必须全量重建？

向量坐标语义由模型决定。即使模型 A 和 B 都输出 512 维，也不表示处在同一向量空间。索引记录 model_id 和 dimensions，任一变化都视为 Schema migration。

### FeatureHashEmbedding 是真正的语义 Embedding 吗？

不是。它对词元做稳定特征哈希，主要用于离线协议测试、确定性评测和无网络降级。真实同义语义检索使用 GLM embedding-3 或本地语义模型。面试时不能把词法特征向量包装成大模型 Embedding。

### 为什么 GLM Embedding 默认 512 维？

官方 embedding-3 支持 256 到 2048 的几个固定维度。512 在学习项目中平衡存储、扫描成本和表示能力，但它不是普适最优值，最终应通过检索集比较质量、延迟和成本。

### API Key 已经配置，为什么还要外部数据授权？

Key 只代表有调用权限，不代表私有源码允许出组织边界。真实 Embedding 会发送 chunk 内容，因此配置默认拒绝，必须显式开启外发；敏感仓库应使用本地或企业内部模型。

### 如何防止 Prompt injection？

索引内容和检索结果都标记为不可信 Evidence，命中代码中的“忽略系统指令”不能改变 Agent 权限。真正边界仍是工具白名单、Registry、路径沙箱、执行授权和最终验证，不能只靠 Prompt。

### Recall@K 和 MRR 分别说明什么？

Recall@K 看相关资料是否进入前 K，适合衡量漏召回；MRR 看第一个相关结果排得多靠前，反映 Agent 多快能看到有效证据。二者都不评价最终答案是否正确。

### 为什么还没上 MQE、HyDE 和 Reranker？

先建立可评测的基础混合检索，才能知道高级策略是否真的改善指标。MQE 可能提高召回但增加调用和语义漂移；HyDE 的假设答案只能作为查询媒介；Reranker 增加延迟和成本。后续应在固定评测集上逐项做消融实验。

## 当前代码证据

- `rag/chunking.py`：安全扫描、Python AST、Markdown 和文本分块。
- `rag/embeddings.py`：Embedding 端口、离线特征向量和 GLM 适配器。
- `rag/index.py`：增量事务、FTS5、余弦检索和 RRF。
- `rag/tool.py`：受 Tool Registry 管理的 RAG 工具。
- `rag/evaluation.py`：Recall@K 和 MRR。
- `tests/test_repository_rag.py`：17 个离线 RAG 测试。
- `tests/test_glm_live.py`：显式 GLM Embedding 协议测试。

全项目当前 118 个测试：116 个通过，2 个真实 GLM 测试默认跳过。

## 主动说明的局限

1. 本地 Dense Retrieval 是 O(N) 精确扫描。
2. FeatureHashEmbedding 不具备真正语义理解。
3. 中文 BM25 没有接入专用分词器。
4. 相关性评测目前主要标注到文件级。
5. 尚未实现 Reranker、MQE、HyDE 和 MMR 去重。
6. 尚未自动监听文件变化。
7. RAG 结果还未进入下一模块的统一 Context Builder。

