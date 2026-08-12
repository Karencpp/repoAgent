# 代码库 RAG 链路

## 建库链路

```text
显式 ProjectContext
  → 开始前校验 repo revision
  → 安全扫描白名单文本文件
       ├─ 跳过缓存和虚拟环境
       ├─ 跳过符号链接
       ├─ 限制文件数和单文件大小
       └─ 计算文件 SHA-256
  → 与 rag_documents 比较
       ├─ unchanged：复用
       ├─ changed/new：重新分块
       └─ deleted：准备删除
  → 结构感知分块
       ├─ Python AST 顶层符号
       ├─ Python 模块级代码
       ├─ Markdown heading path
       └─ 普通文本行窗口
  → 批量 Embedding
  → 校验数量、维度、模型空间
  → 再次校验 repo revision
  → SQLite 单事务更新
       ├─ rag_documents
       ├─ rag_chunks + embedding
       ├─ rag_chunks_fts
       └─ rag_index_state
```

## 查询链路

```text
query + ProjectContext
  → 校验 project_id 已有索引
  → 校验 index revision == current revision
  → 校验 embedding model + dimensions
  → 候选池
       ├─ FTS5 BM25 lexical ranks
       └─ cosine dense ranks
  → Reciprocal Rank Fusion
  → Top-K RetrievalHit
       ├─ path
       ├─ line range
       ├─ symbol / heading
       ├─ content hash
       ├─ lexical_rank
       └─ dense_rank
  → search_repository_knowledge ToolResult
  → Agent 用 read_file_range / inspect_python 复核
```

## RAG 与精确工具分工

```text
RAG：不知道文件名时，找可能相关的位置
grep：知道精确标识符时，确认所有字符串命中
AST：确认 Python 符号、导入和结构
read_file_range：获取当前 revision 的精确行
pytest：验证运行行为
```

RAG 命中不是最终事实。它的主要价值是提高大仓库中的导航效率。

## 混合召回示例

```text
查询：“恢复登录凭证的逻辑在哪里？”

BM25：
  1. docs/password-reset.md
  2. tests/test_credentials.py

Dense：
  1. src/auth/recovery.py
  2. docs/password-reset.md

RRF：
  1. docs/password-reset.md   两路都命中
  2. src/auth/recovery.py     语义命中
  3. tests/test_credentials.py 关键词命中
```

## 评测链路

```text
RetrievalCase(query, relevant_paths)
  → hybrid search Top-K
  → Recall@K
  → Reciprocal Rank
  → 宏平均 Recall@K / MRR
```

