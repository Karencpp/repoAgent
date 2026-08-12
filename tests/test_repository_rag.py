from __future__ import annotations

import json
from pathlib import Path
import shutil
import sys
import unittest
from typing import Sequence
from uuid import uuid4

import httpx


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
TEST_TEMP_ROOT = PROJECT_ROOT / ".rag-test-tmp"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from repo_agent.llm import (
    LLMAuthenticationError,
    LLMConfigurationError,
    LLMResponseError,
)
from repo_agent.projects import ProjectContext, ProjectContextResolver, ProjectRegistry
from repo_agent.rag import (
    FeatureHashEmbeddingClient,
    GLMEmbeddingClient,
    GLMEmbeddingConfig,
    RAGRevisionMismatchError,
    RepositoryChunker,
    RetrievalCase,
    SQLiteRAGIndex,
    evaluate_retrieval,
    register_repository_rag_tool,
)
from repo_agent.tools import ToolRegistry


class RecordingEmbeddingClient:
    """记录向量化文本数量的确定性测试客户端。"""

    def __init__(self, model_id: str = "recording-v1", dimensions: int = 128) -> None:
        self._model_id = model_id
        self._delegate = FeatureHashEmbeddingClient(dimensions)
        self.batches: list[tuple[str, ...]] = []

    @property
    def model_id(self) -> str:
        return self._model_id

    @property
    def dimensions(self) -> int:
        return self._delegate.dimensions

    def embed_texts(
        self,
        texts: Sequence[str],
    ) -> tuple[tuple[float, ...], ...]:
        normalized = tuple(texts)
        self.batches.append(normalized)
        return self._delegate.embed_texts(normalized)


class SemanticFixtureEmbeddingClient:
    """为混合检索测试提供可预测的语义映射。"""

    model_id = "semantic-fixture-v1"
    dimensions = 3

    def embed_texts(
        self,
        texts: Sequence[str],
    ) -> tuple[tuple[float, ...], ...]:
        vectors: list[tuple[float, ...]] = []
        for text in texts:
            lowered = text.casefold()
            if any(term in lowered for term in ("credential", "password", "登录凭证")):
                vectors.append((1.0, 0.0, 0.0))
            elif any(term in lowered for term in ("invoice", "billing", "账单")):
                vectors.append((0.0, 1.0, 0.0))
            else:
                vectors.append((0.0, 0.0, 1.0))
        return tuple(vectors)


class RepositoryRAGTests(unittest.TestCase):
    def setUp(self) -> None:
        TEST_TEMP_ROOT.mkdir(parents=True, exist_ok=True)
        self.root = TEST_TEMP_ROOT / f"case-{uuid4().hex}"
        self.repo = self.root / "target-repo"
        self.state = self.root / "state"
        self.repo.mkdir(parents=True)
        self.resolver = ProjectContextResolver(
            ProjectRegistry(self.state / "projects.json")
        )

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def context(self, repo: Path | None = None) -> ProjectContext:
        """重新读取目标仓库版本。"""

        return self.resolver.resolve(repo=repo or self.repo)

    def build_index(
        self,
        embedding_client: object | None = None,
    ) -> SQLiteRAGIndex:
        """创建测试使用的独立 SQLite 索引。"""

        return SQLiteRAGIndex(
            self.state / "rag.sqlite3",
            embedding_client or FeatureHashEmbeddingClient(128),
        )

    def test_python_chunking_preserves_symbols_module_text_and_citations(self) -> None:
        source = (
            "import os\n"
            "SETTING = 'before'\n\n"
            "@staticmethod\n"
            "def load_invoice():\n"
            "    return 'invoice'\n\n"
            "AFTER = 'kept'\n\n"
            "class BillingService:\n"
            "    def charge(self):\n"
            "        return True\n\n"
            "TAIL = 'also kept'\n"
        )
        path = self.repo / "billing.py"
        path.write_text(source, encoding="utf-8")
        context = self.context()
        scanned = RepositoryChunker().scan(context)

        chunks = RepositoryChunker().chunk(scanned.files[0])

        symbols = {chunk.symbol for chunk in chunks if chunk.symbol}
        combined = "\n".join(chunk.content for chunk in chunks)
        self.assertEqual(symbols, {"load_invoice", "BillingService"})
        self.assertIn("SETTING", combined)
        self.assertIn("AFTER", combined)
        self.assertIn("TAIL", combined)
        self.assertTrue(all(chunk.citation.startswith("billing.py:") for chunk in chunks))

    def test_markdown_chunking_preserves_heading_hierarchy(self) -> None:
        (self.repo / "guide.md").write_text(
            "# 运维手册\n介绍\n## 登录故障\n重置登录凭证\n### 审计\n记录操作人\n",
            encoding="utf-8",
        )
        scanned = RepositoryChunker().scan(self.context())

        chunks = RepositoryChunker().chunk(scanned.files[0])

        paths = [chunk.heading_path for chunk in chunks]
        self.assertIn(("运维手册", "登录故障"), paths)
        self.assertIn(("运维手册", "登录故障", "审计"), paths)

    def test_scan_skips_links_unsupported_and_large_files(self) -> None:
        (self.repo / "ok.py").write_text("VALUE = 1\n", encoding="utf-8")
        (self.repo / "image.bin").write_bytes(b"binary")
        cache = self.repo / "__pycache__"
        cache.mkdir()
        (cache / "ignored.py").write_text("BAD = 1\n", encoding="utf-8")

        scan = RepositoryChunker().scan(self.context())

        self.assertEqual([item.path for item in scan.files], ["ok.py"])
        self.assertGreaterEqual(scan.skipped_files, 1)

    def test_incremental_index_only_reembeds_changed_files_and_removes_deleted(self) -> None:
        first = self.repo / "first.py"
        second = self.repo / "second.py"
        first.write_text("def first():\n    return 1\n", encoding="utf-8")
        second.write_text("def second():\n    return 2\n", encoding="utf-8")
        embedding = RecordingEmbeddingClient()
        index = self.build_index(embedding)
        first_context = self.context()

        initial = index.index_repository(first_context)
        unchanged = index.index_repository(first_context)
        first.write_text("def first():\n    return 100\n", encoding="utf-8")
        changed = index.index_repository(self.context())
        second.unlink()
        deleted = index.index_repository(self.context())

        self.assertEqual(initial.indexed_files, 2)
        self.assertEqual(unchanged.indexed_files, 0)
        self.assertEqual(changed.indexed_files, 1)
        self.assertEqual(deleted.deleted_files, 1)
        self.assertEqual(index.count_chunks(first_context.project_id), 1)
        self.assertEqual(embedding.batches[1], ())
        index.close()

    def test_embedding_model_change_forces_full_reindex(self) -> None:
        (self.repo / "service.py").write_text(
            "class Service:\n    pass\n",
            encoding="utf-8",
        )
        context = self.context()
        first = self.build_index(RecordingEmbeddingClient("model-a"))
        first.index_repository(context)
        first.close()
        second_client = RecordingEmbeddingClient("model-b")
        second = self.build_index(second_client)

        report = second.index_repository(context)

        self.assertEqual(report.indexed_files, 1)
        self.assertGreater(len(second_client.batches[0]), 0)
        second.close()

    def test_lexical_search_returns_exact_source_and_revision(self) -> None:
        (self.repo / "handlers.py").write_text(
            "class BillingService:\n    pass\n",
            encoding="utf-8",
        )
        (self.repo / "auth.py").write_text(
            "class LoginService:\n    pass\n",
            encoding="utf-8",
        )
        context = self.context()
        index = self.build_index()
        index.index_repository(context)

        result = index.search(context, "BillingService", mode="lexical", top_k=2)

        self.assertEqual(result.hits[0].path, "handlers.py")
        self.assertEqual(result.repo_revision, context.revision)
        self.assertRegex(result.hits[0].citation, r"handlers\.py:\d+-\d+")
        self.assertIsNotNone(result.hits[0].lexical_rank)
        self.assertIsNone(result.hits[0].dense_rank)
        index.close()

    def test_dense_search_can_bridge_different_wording(self) -> None:
        (self.repo / "auth.md").write_text(
            "# Account\nReset a password after identity verification.\n",
            encoding="utf-8",
        )
        (self.repo / "billing.md").write_text(
            "# Invoice\nCreate a monthly invoice.\n",
            encoding="utf-8",
        )
        context = self.context()
        index = self.build_index(SemanticFixtureEmbeddingClient())
        index.index_repository(context)

        result = index.search(context, "如何恢复登录凭证", mode="dense", top_k=1)

        self.assertEqual(result.hits[0].path, "auth.md")
        self.assertIsNotNone(result.hits[0].dense_rank)
        index.close()

    def test_hybrid_search_records_both_ranks(self) -> None:
        (self.repo / "auth.md").write_text(
            "# Password\nReset a password safely.\n",
            encoding="utf-8",
        )
        context = self.context()
        index = self.build_index(SemanticFixtureEmbeddingClient())
        index.index_repository(context)

        result = index.search(context, "password", mode="hybrid", top_k=1)

        self.assertEqual(result.hits[0].path, "auth.md")
        self.assertEqual(result.hits[0].lexical_rank, 1)
        self.assertEqual(result.hits[0].dense_rank, 1)
        index.close()

    def test_search_rejects_stale_repository_revision(self) -> None:
        file_path = self.repo / "service.py"
        file_path.write_text("VALUE = 1\n", encoding="utf-8")
        original = self.context()
        index = self.build_index()
        index.index_repository(original)
        file_path.write_text("VALUE = 222\n", encoding="utf-8")
        changed = self.context()

        with self.assertRaises(RAGRevisionMismatchError):
            index.search(changed, "VALUE")
        index.close()

    def test_indexing_rejects_stale_project_context(self) -> None:
        file_path = self.repo / "service.py"
        file_path.write_text("VALUE = 1\n", encoding="utf-8")
        stale = self.context()
        file_path.write_text("VALUE = 9999\n", encoding="utf-8")
        index = self.build_index()

        with self.assertRaises(RAGRevisionMismatchError):
            index.index_repository(stale)
        self.assertEqual(index.count_chunks(stale.project_id), 0)
        index.close()

    def test_same_database_isolates_projects(self) -> None:
        other_repo = self.root / "other-repo"
        other_repo.mkdir()
        (self.repo / "only_a.py").write_text("ALPHA_SECRET = 1\n", encoding="utf-8")
        (other_repo / "only_b.py").write_text("BETA_SECRET = 2\n", encoding="utf-8")
        context_a = self.context(self.repo)
        context_b = self.context(other_repo)
        index = self.build_index()
        index.index_repository(context_a)
        index.index_repository(context_b)

        result_a = index.search(context_a, "BETA_SECRET", mode="lexical")
        result_b = index.search(context_b, "BETA_SECRET", mode="lexical")

        self.assertEqual(result_a.hits, ())
        self.assertEqual(result_b.hits[0].path, "only_b.py")
        self.assertNotEqual(context_a.project_id, context_b.project_id)
        index.close()

    def test_rag_tool_joins_registry_and_returns_structured_citations(self) -> None:
        (self.repo / "service.py").write_text(
            "class PaymentGateway:\n    pass\n",
            encoding="utf-8",
        )
        context = self.context()
        index = self.build_index()
        index.index_repository(context)
        registry = ToolRegistry()
        register_repository_rag_tool(registry, index, context)

        result = registry.dispatch(
            "search_repository_knowledge",
            {"query": "PaymentGateway", "top_k": 3, "mode": "hybrid"},
        )

        self.assertTrue(result.ok)
        self.assertEqual(result.data.project_id, context.project_id)
        self.assertIn("service.py", result.metadata["citations"][0])
        definition = registry.model_tools()[0]
        self.assertFalse(definition.executes_project_code)
        self.assertIn("mode", definition.input_schema["properties"])
        index.close()

    def test_retrieval_evaluation_calculates_recall_and_mrr(self) -> None:
        (self.repo / "billing.py").write_text(
            "class InvoiceCalculator:\n    pass\n",
            encoding="utf-8",
        )
        (self.repo / "auth.py").write_text(
            "class PasswordResetService:\n    pass\n",
            encoding="utf-8",
        )
        context = self.context()
        index = self.build_index()
        index.index_repository(context)

        report = evaluate_retrieval(
            index,
            context,
            (
                RetrievalCase(
                    case_id="billing",
                    query="InvoiceCalculator",
                    relevant_paths=("billing.py",),
                ),
                RetrievalCase(
                    case_id="auth",
                    query="PasswordResetService",
                    relevant_paths=("auth.py",),
                ),
            ),
            top_k=1,
        )

        self.assertEqual(report.mean_recall_at_k, 1.0)
        self.assertEqual(report.mean_reciprocal_rank, 1.0)
        index.close()


class GLMEmbeddingAdapterTests(unittest.TestCase):
    def test_retries_incomplete_response_then_succeeds(self) -> None:
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise httpx.RemoteProtocolError(
                    "incomplete chunked read",
                    request=request,
                )
            return httpx.Response(
                200,
                json={
                    "model": "embedding-3",
                    "data": [{"index": 0, "embedding": [1.0] * 256}],
                },
            )

        client = GLMEmbeddingClient(
            GLMEmbeddingConfig(
                api_key="dummy-key",
                dimensions=256,
                max_retries=1,
                retry_backoff_seconds=0,
                allow_external_data=True,
            ),
            http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        )

        vectors = client.embed_texts(("retry me",))

        self.assertEqual(calls, 2)
        self.assertEqual(len(vectors), 1)

    def test_batches_requests_and_restores_provider_order(self) -> None:
        captured: list[dict[str, object]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            captured.append(body)
            data = [
                {
                    "index": index,
                    "object": "embedding",
                    "embedding": [float(index + 1)] * 256,
                }
                for index, _ in enumerate(body["input"])
            ]
            data.reverse()
            return httpx.Response(
                200,
                json={"model": "embedding-3", "object": "list", "data": data},
            )

        client = GLMEmbeddingClient(
            GLMEmbeddingConfig(
                api_key="dummy-key",
                dimensions=256,
                batch_size=2,
                base_url="https://example.test/api/paas/v4",
                allow_external_data=True,
            ),
            http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        )

        vectors = client.embed_texts(("one", "two", "three"))

        self.assertEqual(len(captured), 2)
        self.assertEqual(captured[0]["model"], "embedding-3")
        self.assertEqual(captured[0]["dimensions"], 256)
        self.assertEqual(vectors[0][0], 1.0)
        self.assertEqual(vectors[1][0], 2.0)
        self.assertEqual(vectors[2][0], 1.0)

    def test_embedding_auth_error_does_not_leak_secret(self) -> None:
        secret = "embedding-secret-not-for-logs"

        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(
                401,
                json={"error": {"message": f"invalid {secret}"}},
            )

        client = GLMEmbeddingClient(
            GLMEmbeddingConfig(
                api_key=secret,
                dimensions=256,
                allow_external_data=True,
            ),
            http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        )

        with self.assertRaises(LLMAuthenticationError) as caught:
            client.embed_texts(("test",))
        self.assertNotIn(secret, str(caught.exception))

    def test_embedding_rejects_wrong_dimension(self) -> None:
        client = GLMEmbeddingClient(
            GLMEmbeddingConfig(
                api_key="dummy",
                dimensions=256,
                allow_external_data=True,
            ),
            http_client=httpx.Client(
                transport=httpx.MockTransport(
                    lambda _: httpx.Response(
                        200,
                        json={
                            "model": "embedding-3",
                            "data": [{"index": 0, "embedding": [1.0, 2.0]}],
                        },
                    )
                )
            ),
        )

        with self.assertRaises(LLMResponseError):
            client.embed_texts(("test",))

    def test_external_embedding_requires_explicit_data_authorization(self) -> None:
        client = GLMEmbeddingClient(
            GLMEmbeddingConfig(api_key="dummy", dimensions=256),
            http_client=httpx.Client(
                transport=httpx.MockTransport(
                    lambda _: self.fail("未授权时不应发起网络请求")
                )
            ),
        )

        with self.assertRaisesRegex(
            LLMConfigurationError,
            "未获得外部数据授权",
        ):
            client.embed_texts(("private source code",))


if __name__ == "__main__":
    unittest.main()
