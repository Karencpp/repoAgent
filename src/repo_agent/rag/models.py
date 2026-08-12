"""代码库 RAG 的结构化领域模型。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class RAGModel(BaseModel):
    """RAG 领域对象的严格公共配置。"""

    model_config = ConfigDict(extra="forbid", frozen=True)


class RepositoryChunkDraft(RAGModel):
    """尚未绑定项目和向量的代码库分块。"""

    path: str = Field(min_length=1, max_length=1_000)
    kind: Literal[
        "python_symbol",
        "python_module",
        "markdown_section",
        "text",
    ]
    language: str = Field(min_length=1, max_length=50)
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    content: str = Field(min_length=1, max_length=20_000)
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    symbol: str | None = Field(default=None, max_length=500)
    heading_path: tuple[str, ...] = Field(default=(), max_length=20)

    @model_validator(mode="after")
    def validate_line_range(self) -> "RepositoryChunkDraft":
        """拒绝倒置的来源行号。"""

        if self.end_line < self.start_line:
            raise ValueError("end_line 不能小于 start_line")
        return self

    @property
    def citation(self) -> str:
        """返回可以交给精确读取工具复核的来源引用。"""

        return f"{self.path}:{self.start_line}-{self.end_line}"

    def embedding_text(self) -> str:
        """将路径和结构信息加入向量文本。"""

        descriptors = [f"文件：{self.path}", f"类型：{self.kind}"]
        if self.symbol:
            descriptors.append(f"符号：{self.symbol}")
        if self.heading_path:
            descriptors.append(f"标题：{' > '.join(self.heading_path)}")
        descriptors.append(self.content)
        return "\n".join(descriptors)


class IndexingReport(RAGModel):
    """一次增量索引的可审计统计。"""

    project_id: str
    repo_revision: str
    embedding_model: str
    embedding_dimensions: int = Field(ge=1)
    scanned_files: int = Field(ge=0)
    indexed_files: int = Field(ge=0)
    unchanged_files: int = Field(ge=0)
    deleted_files: int = Field(ge=0)
    skipped_files: int = Field(ge=0)
    written_chunks: int = Field(ge=0)


class RetrievalHit(RAGModel):
    """带来源、排名和融合分数的检索命中。"""

    chunk_id: str
    path: str
    kind: str
    language: str
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    content: str
    content_hash: str
    symbol: str | None = None
    heading_path: tuple[str, ...] = ()
    citation: str
    score: float = Field(ge=0)
    lexical_rank: int | None = Field(default=None, ge=1)
    dense_rank: int | None = Field(default=None, ge=1)


class RetrievalResult(RAGModel):
    """一次绑定项目版本的混合检索结果。"""

    project_id: str
    repo_revision: str
    query: str
    hits: tuple[RetrievalHit, ...]
    lexical_candidates: int = Field(ge=0)
    dense_candidates: int = Field(ge=0)
    embedding_model: str


class RetrievalCase(RAGModel):
    """一个带相关文件标注的离线检索用例。"""

    case_id: str = Field(min_length=1, max_length=100)
    query: str = Field(min_length=1, max_length=2_000)
    relevant_paths: tuple[str, ...] = Field(min_length=1)


class RetrievalCaseScore(RAGModel):
    """单个检索用例的 Recall@K 和倒数排名。"""

    case_id: str
    retrieved_paths: tuple[str, ...]
    recall_at_k: float = Field(ge=0, le=1)
    reciprocal_rank: float = Field(ge=0, le=1)


class RetrievalEvaluationReport(RAGModel):
    """检索集上的宏平均质量指标。"""

    top_k: int = Field(ge=1)
    case_scores: tuple[RetrievalCaseScore, ...]
    mean_recall_at_k: float = Field(ge=0, le=1)
    mean_reciprocal_rank: float = Field(ge=0, le=1)
