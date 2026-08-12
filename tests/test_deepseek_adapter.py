from __future__ import annotations

import json
from pathlib import Path
import sys
import unittest

import httpx


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from repo_agent.llm import (
    ChatMessage,
    DeepSeekChatClient,
    DeepSeekConfig,
    LLMAuthenticationError,
    LLMConfigurationError,
    LLMStructuredOutputError,
    StructuredJSONRequest,
    resolve_llm_provider,
)


def request() -> StructuredJSONRequest:
    """创建要求 JSON 对象的最小结构化请求。"""

    return StructuredJSONRequest(
        messages=(
            ChatMessage(role="system", content="只返回 JSON 对象"),
            ChatMessage(role="user", content="返回状态"),
        ),
        schema_name="status",
        json_schema={"type": "object"},
    )


class DeepSeekAdapterTests(unittest.TestCase):
    def test_config_reads_dedicated_environment_and_hides_secret(self) -> None:
        config = DeepSeekConfig.from_env(
            {
                "DEEPSEEK_API_KEY": "deepseek-test-secret",
                "DEEPSEEK_MODEL": "deepseek-test",
                "DEEPSEEK_BASE_URL": "https://example.test/v1/",
            }
        )

        self.assertEqual(config.model, "deepseek-test")
        self.assertEqual(config.endpoint, "https://example.test/v1/chat/completions")
        self.assertNotIn("deepseek-test-secret", repr(config))

    def test_missing_key_fails_before_network(self) -> None:
        with self.assertRaises(LLMConfigurationError):
            DeepSeekConfig.from_env({})

    def test_request_uses_bearer_and_json_output(self) -> None:
        captured: dict[str, object] = {}

        def handler(http_request: httpx.Request) -> httpx.Response:
            captured["authorization"] = http_request.headers["Authorization"]
            captured["body"] = json.loads(http_request.content)
            return httpx.Response(
                200,
                json={
                    "id": "response-1",
                    "model": "deepseek-test",
                    "choices": [
                        {
                            "message": {"content": '{"status":"ok"}'},
                            "finish_reason": "stop",
                        }
                    ],
                },
            )

        client = DeepSeekChatClient(
            DeepSeekConfig(api_key="test-secret", model="deepseek-test"),
            http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        )

        result = client.generate_json(request())

        self.assertEqual(result, {"status": "ok"})
        self.assertEqual(captured["authorization"], "Bearer test-secret")
        body = captured["body"]
        self.assertEqual(body["response_format"], {"type": "json_object"})
        self.assertFalse(body["stream"])

    def test_authentication_error_does_not_leak_key(self) -> None:
        secret = "deepseek-do-not-leak"
        client = DeepSeekChatClient(
            DeepSeekConfig(api_key=secret),
            http_client=httpx.Client(
                transport=httpx.MockTransport(
                    lambda _: httpx.Response(
                        401,
                        json={"error": {"message": f"invalid {secret}"}},
                    )
                )
            ),
        )

        with self.assertRaises(LLMAuthenticationError) as caught:
            client.generate_json(request())

        self.assertNotIn(secret, str(caught.exception))

    def test_truncated_or_empty_json_is_rejected(self) -> None:
        responses = iter(
            (
                httpx.Response(
                    200,
                    json={
                        "choices": [
                            {"message": {"content": "{}"}, "finish_reason": "length"}
                        ]
                    },
                ),
                httpx.Response(
                    200,
                    json={
                        "choices": [
                            {"message": {"content": ""}, "finish_reason": "stop"}
                        ]
                    },
                ),
            )
        )
        client = DeepSeekChatClient(
            DeepSeekConfig(api_key="test-secret"),
            http_client=httpx.Client(
                transport=httpx.MockTransport(lambda _: next(responses))
            ),
        )

        with self.assertRaises(LLMStructuredOutputError):
            client.generate_json(request())
        with self.assertRaises(LLMStructuredOutputError):
            client.generate_json(request())

    def test_provider_selection_is_explicit_and_validated(self) -> None:
        self.assertEqual(resolve_llm_provider("DeepSeek"), "deepseek")
        self.assertEqual(resolve_llm_provider("glm"), "glm")
        with self.assertRaises(LLMConfigurationError):
            resolve_llm_provider("unknown")


if __name__ == "__main__":
    unittest.main()
