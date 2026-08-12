import json

import httpx
import pytest

from repo_agent.llm import LLMConfigurationError, LLMResponseError
from repo_agent.rag import GLMRerankerClient, GLMRerankerConfig


def test_glm_reranker_restores_indexes_and_usage() -> None:
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "results": [
                    {"index": 1, "relevance_score": 0.9},
                    {"index": 0, "relevance_score": 0.2},
                ],
                "usage": {"prompt_tokens": 18, "total_tokens": 18},
                "request_id": "request-1",
            },
        )

    client = GLMRerankerClient(
        GLMRerankerConfig(
            api_key="dummy",
            allow_external_data=True,
        ),
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    result = client.rerank("query", ("first", "second"))

    assert captured["model"] == "rerank"
    assert captured["top_n"] == 2
    assert [score.index for score in result.scores] == [1, 0]
    assert result.total_tokens == 18


def test_glm_reranker_requires_external_data_authorization() -> None:
    client = GLMRerankerClient(
        GLMRerankerConfig(api_key="dummy"),
        http_client=httpx.Client(
            transport=httpx.MockTransport(
                lambda _: pytest.fail("unauthorized request must not be sent")
            )
        ),
    )

    with pytest.raises(LLMConfigurationError, match="外部数据授权"):
        client.rerank("query", ("document",))


def test_glm_reranker_rejects_incomplete_indexes() -> None:
    client = GLMRerankerClient(
        GLMRerankerConfig(api_key="dummy", allow_external_data=True),
        http_client=httpx.Client(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    200,
                    json={
                        "results": [{"index": 0, "relevance_score": 0.8}],
                        "usage": {},
                    },
                )
            )
        ),
    )

    with pytest.raises(LLMResponseError, match="候选索引"):
        client.rerank("query", ("first", "second"))


def test_glm_reranker_rejects_oversized_query_document_pair_locally() -> None:
    client = GLMRerankerClient(
        GLMRerankerConfig(
            api_key="dummy",
            allow_external_data=True,
            max_pair_chars=10,
        ),
        http_client=httpx.Client(
            transport=httpx.MockTransport(
                lambda _: pytest.fail("oversized pair must not be sent")
            )
        ),
    )

    with pytest.raises(ValueError, match="合计"):
        client.rerank("12345", ("678901",))


def test_glm_reranker_retries_transport_failure() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.RemoteProtocolError("incomplete response", request=request)
        return httpx.Response(
            200,
            json={
                "results": [{"index": 0, "relevance_score": 0.8}],
                "usage": {},
            },
        )

    client = GLMRerankerClient(
        GLMRerankerConfig(
            api_key="dummy",
            allow_external_data=True,
            max_retries=1,
            retry_backoff_seconds=0,
        ),
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    assert client.rerank("query", ("document",)).scores[0].index == 0
    assert calls == 2
