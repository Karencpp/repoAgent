from types import SimpleNamespace

from repo_agent.rag.postgres import PostgresRAGIndex


class _EmbeddingMustNotRun:
    model_id = "unused"
    dimensions = 256

    def embed_texts(self, texts):
        raise AssertionError("lexical mode must not call the embedding provider")


class _Cursor:
    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None

    def execute(self, *_args, **_kwargs):
        return self

    def fetchall(self):
        return []


class _Connection:
    def cursor(self):
        return _Cursor()


def test_postgres_lexical_mode_does_not_call_embedding_provider() -> None:
    index = object.__new__(PostgresRAGIndex)
    index.embedding_client = _EmbeddingMustNotRun()
    index._connection = _Connection()
    index._vector_type = "vector(256)"
    context = SimpleNamespace(project_id="project-1", revision="revision-1")

    lexical_ids, dense_ids = index._rank_ids(
        context,
        "find QuerySet filter",
        20,
        "lexical",
    )

    assert lexical_ids == []
    assert dense_ids == []
