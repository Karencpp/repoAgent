from __future__ import annotations

from pathlib import Path
import shutil
import sys
import unittest
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
TEST_TEMP_ROOT = PROJECT_ROOT / ".production-evolution-test-tmp"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from repo_agent.cli import build_parser
from repo_agent.mcp import (
    MCPHostConfig,
    MCPRegistryServerConfig,
    MCPServerPolicy,
    MCPToolPolicy,
    attach_configured_mcp_servers,
)
from repo_agent.projects import ProjectContextResolver, ProjectRegistry
from repo_agent.rag import FeatureHashEmbeddingClient, SQLiteRAGIndex
from repo_agent.storage import InfrastructureFactory, StorageConfig
from repo_agent.tools import ToolDefinition, ToolRegistry, ToolResult
from repo_agent.workflow import (
    DeterministicFinalAnswerSynthesizer,
    EvaluationResult,
    RepoAgentRunResult,
    StepExecution,
)


class LookupArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1)


class ProductionEvolutionP2P3Tests(unittest.TestCase):
    def setUp(self) -> None:
        TEST_TEMP_ROOT.mkdir(parents=True, exist_ok=True)
        self.test_root = TEST_TEMP_ROOT / uuid4().hex
        self.repo_root = self.test_root / "target"
        (self.repo_root / "src").mkdir(parents=True)
        (self.repo_root / "src" / "demo.py").write_text(
            "def entry():\n    return 'ok'\n",
            encoding="utf-8",
        )
        self.context = ProjectContextResolver(
            ProjectRegistry(self.test_root / "projects.json")
        ).resolve(repo=self.repo_root)

    def tearDown(self) -> None:
        shutil.rmtree(self.test_root, ignore_errors=True)

    def test_final_answer_validates_citations_against_current_revision(self) -> None:
        result = RepoAgentRunResult(
            run_id="run-1",
            thread_id="thread-1",
            project_id=self.context.project_id,
            repo_root=str(self.context.repo_root),
            repo_revision=self.context.revision,
            user_goal="解释入口",
            mode="diagnose",
            status="completed",
            plan=None,
            plan_history=(),
            step_results=(
                StepExecution(
                    step_id="inspect",
                    status="completed",
                    summary="入口位于 src/demo.py:1-2",
                    react_status="completed",
                    stop_reason="完成",
                    iterations=1,
                    tool_calls=1,
                ),
            ),
            evaluation=EvaluationResult(
                passed=True,
                summary="证据满足问题",
                evidence=("src/demo.py:1-2",),
            ),
            evaluation_history=(),
            reflection_history=(),
            reflection_count=0,
            replan_count=0,
            final_report="旧报告",
            stop_reason="任务通过评估",
            trace=(),
        )

        answer = DeterministicFinalAnswerSynthesizer().synthesize(
            self.context,
            result,
        )

        self.assertEqual(answer.citations[0].label, "src/demo.py:1-2")
        self.assertTrue(answer.claims[0].supported)
        self.assertIn("## 引用", answer.answer)

    def test_mcp_config_attaches_registry_server_tools(self) -> None:
        registry = ToolRegistry()
        registry.register(
            ToolDefinition(
                name="lookup",
                description="查询本地记录。",
                access="read",
                executes_project_code=False,
                requires_explicit_authorization=False,
            ),
            LookupArguments,
            lambda arguments: ToolResult.success({"items": [arguments.query]}),
        )
        schema = registry.model_tools()[0].input_schema
        host_config = MCPHostConfig(
            servers=(
                MCPRegistryServerConfig(
                    exported_tools=("lookup",),
                    policy=MCPServerPolicy(
                        server_id="local-registry",
                        tools=(
                            MCPToolPolicy(
                                remote_name="lookup",
                                local_name="mcp_local_lookup",
                                description="查询已审核本地记录。",
                                input_schema=schema,
                                access="read",
                                requires_explicit_authorization=False,
                            ),
                        ),
                    ),
                ),
            )
        )

        _gateway, snapshots = attach_configured_mcp_servers(
            registry=registry,
            config=host_config,
        )
        result = registry.dispatch("mcp_local_lookup", {"query": "42"})

        self.assertEqual(snapshots[0].mapped_tools, ("mcp_local_lookup",))
        self.assertTrue(result.ok)
        self.assertIn("42", str(result.data))

    def test_storage_factory_keeps_sqlite_default(self) -> None:
        factory = InfrastructureFactory(
            StorageConfig(backend="sqlite", sqlite_state_dir=self.test_root / "state"),
            embedding_client=FeatureHashEmbeddingClient(256),
        )
        rag = factory.create_rag_index()
        try:
            self.assertIsInstance(rag, SQLiteRAGIndex)
        finally:
            rag.close()

    def test_cli_parses_memory_and_migration_commands(self) -> None:
        memory = build_parser().parse_args(
            [
                "memory",
                "consolidate",
                "--repo",
                str(self.repo_root),
                "--topic",
                "测试失败模式",
                "--storage-backend",
                "sqlite",
            ]
        )
        migrate = build_parser().parse_args(
            [
                "migrate-state",
                "--sqlite-state-dir",
                str(self.test_root / "state"),
                "--postgres-dsn",
                "postgresql://repo_agent:secret@localhost/repo_agent",
                "--dry-run",
            ]
        )

        self.assertEqual(memory.command, "memory")
        self.assertEqual(memory.memory_command, "consolidate")
        self.assertEqual(migrate.command, "migrate-state")
        self.assertTrue(migrate.dry_run)


if __name__ == "__main__":
    unittest.main()
