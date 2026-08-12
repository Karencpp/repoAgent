from pathlib import Path
from uuid import uuid4

from repo_agent.llm.contracts import LLMResponseError
from repo_agent.rag import RerankResponse, RerankScore

from evals.benchmarks.reranker_api_benchmark import PersistentRerankCache


class _FakeReranker:
    model_id = "fake:rerank"

    def __init__(self, reject_multi: bool = False) -> None:
        self.reject_multi = reject_multi
        self.calls: list[tuple[str, ...]] = []

    def rerank(self, query, documents):
        values = tuple(documents)
        self.calls.append(values)
        if self.reject_multi and len(values) > 1:
            raise LLMResponseError("HTTP 400: query+document超长")
        return RerankResponse(
            scores=tuple(
                RerankScore(index=index, relevance_score=float(len(value)))
                for index, value in enumerate(values)
            ),
            prompt_tokens=len(values),
            total_tokens=len(values),
        )


def _cache_path() -> Path:
    path = (
        Path(__file__).resolve().parents[1]
        / "output"
        / "benchmarks"
        / "state"
        / f"test-rerank-cache-{uuid4().hex}.sqlite3"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def test_rerank_cache_batches_and_reuses_scores() -> None:
    path = _cache_path()
    delegate = _FakeReranker()
    cache = PersistentRerankCache(
        delegate,
        path,
        max_batch_documents=2,
        max_batch_chars=1_000,
    )
    try:
        first = cache.scores("query", ("a", "bb", "ccc", "dddd", "eeeee"))
        second = cache.scores("query", ("a", "bb"))
    finally:
        cache.close()
        path.unlink(missing_ok=True)

    assert first == (1.0, 2.0, 3.0, 4.0, 5.0)
    assert second == first[:2]
    assert [len(call) for call in delegate.calls] == [2, 2, 1]
    assert cache.api_request_count == 3
    assert cache.cache_hit_document_count == 2


def test_rerank_cache_bisects_provider_length_rejection() -> None:
    path = _cache_path()
    delegate = _FakeReranker(reject_multi=True)
    cache = PersistentRerankCache(delegate, path)
    try:
        scores = cache.scores("query", ("a", "bb", "ccc"))
    finally:
        cache.close()
        path.unlink(missing_ok=True)

    assert scores == (1.0, 2.0, 3.0)
    assert cache.api_request_count == 3
    assert cache.rejected_request_count == 2
