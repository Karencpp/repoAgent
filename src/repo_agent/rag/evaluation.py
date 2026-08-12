"""代码库检索质量的离线评测。"""

from __future__ import annotations

from typing import Sequence

from repo_agent.projects import ProjectContext

from .index import SQLiteRAGIndex
from .models import (
    RetrievalCase,
    RetrievalCaseScore,
    RetrievalEvaluationReport,
)


def evaluate_retrieval(
    index: SQLiteRAGIndex,
    context: ProjectContext,
    cases: Sequence[RetrievalCase],
    *,
    top_k: int = 5,
) -> RetrievalEvaluationReport:
    """计算宏平均 Recall@K 和 MRR。"""

    if not cases:
        raise ValueError("检索评测至少需要一个用例")
    scores: list[RetrievalCaseScore] = []
    for case in cases:
        result = index.search(context, case.query, top_k=top_k)
        retrieved_paths = tuple(hit.path for hit in result.hits)
        relevant = set(case.relevant_paths)
        found = relevant.intersection(retrieved_paths)
        first_rank = next(
            (
                rank
                for rank, path in enumerate(retrieved_paths, start=1)
                if path in relevant
            ),
            None,
        )
        scores.append(
            RetrievalCaseScore(
                case_id=case.case_id,
                retrieved_paths=retrieved_paths,
                recall_at_k=len(found) / len(relevant),
                reciprocal_rank=(1.0 / first_rank if first_rank is not None else 0.0),
            )
        )
    return RetrievalEvaluationReport(
        top_k=top_k,
        case_scores=tuple(scores),
        mean_recall_at_k=sum(item.recall_at_k for item in scores) / len(scores),
        mean_reciprocal_rank=(
            sum(item.reciprocal_rank for item in scores) / len(scores)
        ),
    )
