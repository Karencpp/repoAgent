from types import SimpleNamespace
from uuid import uuid4

from pathlib import Path

from evals.benchmarks.production_retrieval_benchmark import (
    PersistentCachingEmbeddingClient,
)


class _FakeEmbeddingClient:
    model_id = "fake:embedding:3"
    dimensions = 3
    config = SimpleNamespace(batch_size=2)

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    def embed_texts(self, texts):
        values = tuple(texts)
        self.calls.append(values)
        return tuple(
            (float(len(value)), float(index), 1.0)
            for index, value in enumerate(values)
        )


def test_persistent_cache_batches_and_reuses_real_results() -> None:
    delegate = _FakeEmbeddingClient()
    cache_path = (
        Path(__file__).resolve().parents[1]
        / "output"
        / "benchmarks"
        / "state"
        / f"test-embedding-cache-{uuid4().hex}.sqlite3"
    )
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache = PersistentCachingEmbeddingClient(delegate, cache_path)
    try:
        first = cache.embed_texts(("alpha", "beta", "gamma", "alpha"))
        second = cache.embed_texts(("gamma", "alpha"))
    finally:
        cache.close()

    assert len(delegate.calls) == 2
    assert all(len(call) <= 2 for call in delegate.calls)
    assert first[0] == first[3]
    assert second == (first[2], first[0])
    assert cache.api_request_count == 2
    assert cache.embedded_text_count == 3
    assert cache.cache_hit_text_count == 2

    reopened_delegate = _FakeEmbeddingClient()
    reopened = PersistentCachingEmbeddingClient(reopened_delegate, cache_path)
    try:
        assert reopened.embed_texts(("alpha", "beta", "gamma")) == first[:3]
    finally:
        reopened.close()
        cache_path.unlink(missing_ok=True)
    assert reopened_delegate.calls == []
