from __future__ import annotations

import os
from pathlib import Path
import sys
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from repo_agent.llm import ChatMessage, GLMChatClient, GLMConfig, StructuredJSONRequest
from repo_agent.rag import GLMEmbeddingClient, GLMEmbeddingConfig


LIVE_GLM_ENABLED = (
    os.getenv("RUN_GLM_INTEGRATION") == "rotated-key-confirmed"
    and bool(os.getenv("ZHIPUAI_API_KEY"))
)


@unittest.skipUnless(
    LIVE_GLM_ENABLED,
    "需要新密钥，并显式设置 RUN_GLM_INTEGRATION=rotated-key-confirmed",
)
class LiveGLMIntegrationTests(unittest.TestCase):
    def test_glm_returns_a_schema_shaped_object(self) -> None:
        """真实调用只验证供应商协议，不承担普通回归测试职责。"""

        schema = {
            "type": "object",
            "properties": {
                "status": {"type": "string", "enum": ["ok"]},
                "provider": {"type": "string"},
            },
            "required": ["status", "provider"],
            "additionalProperties": False,
        }
        with GLMChatClient(GLMConfig.from_env()) as client:
            result = client.generate_json(
                StructuredJSONRequest(
                    messages=(
                        ChatMessage(
                            role="system",
                            content=(
                                "只返回 JSON 对象："
                                '{"status":"ok","provider":"glm"}'
                            ),
                        ),
                        ChatMessage(role="user", content="进行一次最小协议检查"),
                    ),
                    schema_name="health_check",
                    json_schema=schema,
                )
            )

        self.assertEqual(result.get("status"), "ok")
        self.assertIsInstance(result.get("provider"), str)

    def test_glm_embedding_returns_declared_dimensions(self) -> None:
        """真实向量测试只验证数量和维度契约。"""

        config = GLMEmbeddingConfig.from_env()
        if not config.allow_external_data:
            self.skipTest("需要设置 ALLOW_EXTERNAL_CODE_EMBEDDING=true")
        with GLMEmbeddingClient(config) as client:
            vectors = client.embed_texts(("代码库检索协议检查",))

        self.assertEqual(len(vectors), 1)
        self.assertEqual(len(vectors[0]), config.dimensions)


if __name__ == "__main__":
    unittest.main()
