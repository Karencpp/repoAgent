# ADR-008：版本绑定的代码库混合 RAG

- 状态：Accepted
- 日期：2026-07-31
- 模块：Repository RAG

## 背景

仓库工具可以精确搜索字符串和读取指定行，但面对大型代码库时，Agent 不一定知道应该搜索哪个类名、文件名或术语。把全部文件直接放进上下文又会造成 token 成本、注意力噪声和过期内容问题。

本模块需要先从大量代码和文档中召回少量候选，再由现有仓库工具读取当前版本的精确证据。RAG 不是事实裁判，也不替代路径沙箱、AST 工具和测试。

## 决策

### 1. 使用结构感知分块

Python 文件优先按顶层函数、异步函数和类切分，保留：

- 路径；
- 符号名；
- 起止行号；
- 内容哈希；
- 类型和语言。

模块导入、常量和符号之间的代码使用 `python_module` 分块保留，避免只索引函数和类而丢失配置。语法损坏的 Python 文件退回普通文本分块，不让单个解析错误阻断整个索引。

Markdown 按标题层级切分并保存 `heading_path`。其他白名单文本按行和字符预算切分。超大范围继续切成有限字符块，并使用少量行重叠降低边界信息丢失。

### 2. 按文件哈希增量更新

索引记录每个文件的 SHA-256。更新时：

```text
扫描当前文件
  → 新文件或 hash 改变：重新分块和 Embedding
  → hash 相同：复用已有分块和向量
  → 文件消失：删除原文、向量和 FTS 条目
```

Embedding 在 SQLite 写事务之前完成。所有新向量数量和维度校验通过后，才原子替换受影响文件，避免网络中断留下半更新索引。

### 3. 索引同时绑定 project_id 和 repo revision

所有文档、分块、查询和状态都按 `project_id` 隔离。索引完成时保存 `repo_revision`；检索前必须与当前 ProjectContext 完全一致，否则要求先增量更新。

索引开始和向量化结束后还会重新检查仓库 revision。如果代码在耗时的 Embedding 过程中发生改变，本次结果不会写入。

测试发现了一个跨模块边界：目标目录可能位于另一个 Git 仓库的忽略目录下，此时父仓库 clean commit 不能代表目标目录内容。项目身份层现在对“父 Git 中没有任何 tracked file 的所选子目录”使用父 commit 加子目录 manifest，而不是误标成 clean。

### 4. 使用 BM25 与 Dense Retrieval 混合召回

关键词检索使用 SQLite FTS5 的 BM25，适合：

- 精确类名和函数名；
- 错误码；
- 配置项；
- 文件路径和专业术语。

向量检索使用 Embedding 和余弦相似度，适合用户描述与代码命名不同的情况。例如“恢复登录凭证”可以召回包含 `reset password` 的文档。

两路候选使用 Reciprocal Rank Fusion：

```text
score(d) = lexical_weight / (k + lexical_rank)
         + dense_weight / (k + dense_rank)
```

RRF 只依赖名次，不要求把 BM25 分数和余弦分数强行归一化到同一尺度。当前先扩大候选池，再融合并截取 Top-K。

### 5. SQLite 同时保存元数据、向量和 FTS

本项目不面向上线，第一版选择 SQLite：

- 零外部服务，面试和测试环境容易复现；
- 事务边界清楚；
- FTS5 可直接提供 BM25；
- 小型索引可以在 Python 中精确扫描向量，算法容易解释。

代价是向量以 JSON 保存，查询需要 O(N) 扫描，不支持 ANN、分片和服务级并发。当项目分块超过配置的扫描上限时会显式失败，提示切换 Qdrant、pgvector、Milvus 等专业向量后端，而不是悄悄变慢。

### 6. Embedding 使用稳定端口

`EmbeddingClient` 只暴露：

```text
model_id
dimensions
embed_texts(texts)
```

离线测试使用 `FeatureHashEmbeddingClient`。它是确定性词法特征向量，不冒充真正的语义模型，只用于协议测试、可复现评测和无网络降级。

真实后端使用 GLM `embedding-3`，默认 512 维、按官方上限批量调用。索引保存 `model_id + dimensions`，任一变化都会强制重建全部项目分块。相同维度不代表相同语义空间，所以仅检查向量长度不够。

### 7. 外部 Embedding 需要单独的数据外发授权

API Key 只说明调用者能访问供应商，不说明目标代码允许发给外部服务。GLMEmbeddingClient 默认拒绝发送内容，只有显式设置：

```text
ALLOW_EXTERNAL_CODE_EMBEDDING=true
```

才允许真实向量化。私有仓库可继续使用本地模型或组织内部 Embedding 服务。

### 8. 每个命中必须可追溯

RetrievalHit 返回：

- `path:start_line-end_line` 引用；
- chunk_id 和内容哈希；
- 符号或标题层级；
- lexical rank、dense rank 和 RRF score；
- 索引对应的 repo revision。

Agent 应把 RAG 结果视为候选 Evidence，再调用 `read_file_range` 或 AST 工具复核当前文件。工具输出仍是不可信文本，不能把代码注释提升为系统指令。

### 9. 使用检索集评测，而不是凭感觉调 Top-K

`evaluate_retrieval` 接收查询和相关文件标注，计算：

- Recall@K：相关文件有多少进入前 K；
- MRR：第一个相关结果出现得有多靠前。

这只评估 Retrieval，不评估 LLM 最终回答。需要先区分“正确资料没有召回”和“资料已召回但生成错误”。

## 没有选择的方案

### 只使用向量检索

代码中的类名、函数名和错误码非常适合精确词法检索。只用向量会损失这类强信号。

### 只使用 grep 或 BM25

用户描述与代码术语不同、跨语言表达或概念性查询时容易漏召回。

### 固定字符数切所有文件

实现简单，但容易把函数签名和函数体、Markdown 标题和正文拆开，引用也难解释。

### 第一版直接部署 Qdrant

它能提供 ANN、过滤和更大规模服务，但会引入额外进程、集合生命周期和测试基础设施。当前规模用 SQLite 更符合“不面向上线、面向讲清楚”的目标。

### 允许旧 revision 索引继续返回结果

旧结果可以作为历史参考，但代码维护 Agent 很容易把它误当当前事实。第一版选择 fail closed，先更新再检索。

## 当前局限

- SQLite 向量检索是 O(N) 精确扫描，只适合小中型学习项目。
- FeatureHashEmbedding 不是语义模型。
- 尚未实现 Cross-Encoder Reranker、MMR 去重、MQE 和 HyDE。
- 中文关键词分词只使用 FTS5 默认 tokenizer，复杂中文检索更依赖 Dense Retrieval。
- 当前相关性标注以文件路径为主，尚未做到精确 chunk 级 qrels。
- 没有自动监听文件变化，需要运行增量索引。
- 没有对 Git 历史、issue、PR 和外部文档建立独立数据源。

## 官方依据

- [智谱文本嵌入 API](https://docs.bigmodel.cn/api-reference/%E6%A8%A1%E5%9E%8B-api/%E6%96%87%E6%9C%AC%E5%B5%8C%E5%85%A5)
- [Embedding-3 模型说明](https://docs.bigmodel.cn/cn/guide/models/embedding/embedding-3)

官方接口支持 `embedding-3`、字符串数组批量输入，单批最多 64 条，并支持 256、512、1024、2048 维输出。

## 验证证据

新增 17 个离线 RAG 测试和 1 个显式 GLM Embedding live test。全项目当前 118 个测试：116 个通过，2 个真实 GLM 测试默认跳过。

