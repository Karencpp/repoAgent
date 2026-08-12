from __future__ import annotations

from collections import defaultdict
from pathlib import Path
import shutil
import sys
import unittest
from typing import Any, Mapping
from uuid import uuid4


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
TEST_TEMP_ROOT = PROJECT_ROOT / ".application-test-tmp"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from repo_agent.application import RepoAgentApplication, RepoAgentApplicationConfig
from repo_agent.llm import StructuredJSONRequest
from repo_agent.memory import MemoryManager, SQLiteMemoryStore
from repo_agent.maintenance import RepoAgentMaintenanceService
from repo_agent.candidate import CandidatePromotionError
from repo_agent.rag import FeatureHashEmbeddingClient
from repo_agent.tools import ProcessResult


class ApplicationJSONClient:
    """按 Schema 返回最小合法结果的应用级测试模型。"""

    def __init__(self) -> None:
        self.counts: dict[str, int] = defaultdict(int)
        self.requests: list[StructuredJSONRequest] = []

    def generate_json(self, request: StructuredJSONRequest) -> Mapping[str, Any]:
        self.requests.append(request)
        self.counts[request.schema_name] += 1
        if request.schema_name == "execution_plan":
            return {
                "rationale": "先查看文件树，再概括项目入口",
                "steps": [
                    {
                        "id": "inspect-tree",
                        "goal": "查看项目文件结构并解释入口",
                        "expected_evidence": ["文件路径"],
                        "allowed_tools": ["list_files"],
                    }
                ],
            }
        if request.schema_name == "agent_decision":
            if self.counts[request.schema_name] == 1:
                return {
                    "type": "tool_call",
                    "tool_name": "list_files",
                    "arguments": {"max_depth": 3, "max_results": 50},
                    "decision_summary": "读取实际文件树作为证据",
                }
            return {
                "type": "final_answer",
                "answer": "项目入口位于 src/demo/main.py，测试位于 tests。",
                "decision_summary": "已依据文件树完成解释",
            }
        if request.schema_name == "repo_agent_semantic_memory_batch":
            return {"drafts": []}
        if request.schema_name == "candidate_patch_targets":
            return {
                "rationale": "入口函数返回值需要调整",
                "paths": ["src/demo/main.py"],
                "target_tests": ["tests/test_main.py"],
                "regression_targets": ["tests"],
            }
        if request.schema_name == "candidate_patch_draft":
            return {
                "summary": "调整入口函数返回值",
                "changes": [
                    {
                        "path": "src/demo/main.py",
                        "replacement_content": "def main():\n    return 'fixed'\n",
                        "reason": "满足维护目标",
                    }
                ],
            }
        if request.schema_name == "reflection_result":
            return {
                "failure_cause": "缺少仓库证据",
                "corrective_action": "读取文件树后重新回答",
                "should_replan": False,
            }
        raise AssertionError(f"未处理的 Schema：{request.schema_name}")


class PassingProcessRunner:
    """模拟目标测试和回归测试都通过。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    def run(
        self,
        command,
        *,
        cwd: Path,
        timeout_seconds: float,
        output_limit: int,
    ) -> ProcessResult:
        normalized = tuple(command)
        self.calls.append(normalized)
        return ProcessResult(
            command=normalized,
            exit_code=0,
            stdout="1 passed",
            stderr="",
            duration_ms=5,
            timed_out=False,
            output_truncated=False,
        )


class RepoAgentApplicationTests(unittest.TestCase):
    def setUp(self) -> None:
        TEST_TEMP_ROOT.mkdir(parents=True, exist_ok=True)
        self.test_root = TEST_TEMP_ROOT / uuid4().hex
        self.repo_root = self.test_root / "target-repo"
        (self.repo_root / "src" / "demo").mkdir(parents=True)
        (self.repo_root / "tests").mkdir()
        (self.repo_root / "src" / "demo" / "main.py").write_text(
            "def main():\n    return 'ok'\n",
            encoding="utf-8",
        )
        (self.repo_root / "tests" / "test_main.py").write_text(
            "def test_main():\n    assert True\n",
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        shutil.rmtree(self.test_root, ignore_errors=True)

    def test_explain_assembles_real_read_only_pipeline(self) -> None:
        client = ApplicationJSONClient()
        application = RepoAgentApplication(
            RepoAgentApplicationConfig(
                state_dir=self.test_root / "state",
                skills_root=PROJECT_ROOT / "skills",
            ),
            structured_client=client,
        )
        progress: list[str] = []

        result = application.explain(
            "这个项目的入口在哪里？",
            repo=self.repo_root,
            thread_id="explain-entry",
            progress_callback=progress.append,
        )

        self.assertEqual(result.context.repo_root, self.repo_root.resolve())
        self.assertEqual(result.workflow.status, "completed")
        self.assertIn("src/demo/main.py", result.workflow.final_report)
        self.assertIsNotNone(result.indexing)
        self.assertGreaterEqual(result.indexing.scanned_files, 2)
        self.assertIn("diagnose-pytest-failure", result.discovered_skills)
        self.assertTrue(result.memory_decisions)
        self.assertFalse(result.memory_errors)
        self.assertEqual(client.counts["execution_plan"], 1)
        self.assertEqual(client.counts["agent_decision"], 2)
        self.assertEqual(client.counts["repo_agent_semantic_memory_batch"], 1)
        self.assertTrue(result.context_prefetches)
        self.assertTrue(result.context_builds)
        self.assertTrue(
            all(
                item.estimated_input_tokens <= item.input_budget_tokens
                for item in result.context_builds
            )
        )
        self.assertTrue(
            any(item.rag_hits > 0 for item in result.context_prefetches)
        )
        self.assertTrue(any("RAG 索引就绪" in item for item in progress))
        self.assertTrue(any("上下文预检索完成" in item for item in progress))
        self.assertTrue(any("Planner 已生成" in item for item in progress))
        self.assertTrue(any("调用工具 list_files" in item for item in progress))
        self.assertTrue(any("Evaluator 通过" in item for item in progress))
        self.assertEqual(progress[-1], "RepoAgent 本次任务处理完成")

        planner_request = next(
            item for item in client.requests if item.schema_name == "execution_plan"
        )
        planner_content = planner_request.messages[-1].content
        self.assertIn('"source": "rag"', planner_content)
        self.assertIn("search_repository_knowledge", planner_content)
        self.assertIn("search_project_memory", planner_content)
        self.assertNotIn('"name":"run_pytest"', planner_content)

        resumed = application.explain(
            "",
            repo=self.repo_root,
            thread_id="explain-entry",
            resume=True,
        )
        self.assertEqual(resumed.workflow.run_id, result.workflow.run_id)
        self.assertEqual(resumed.workflow.status, "completed")
        self.assertEqual(client.counts["execution_plan"], 1)

    def test_project_selection_never_falls_back_to_current_directory(self) -> None:
        application = RepoAgentApplication(
            RepoAgentApplicationConfig(state_dir=self.test_root / "state"),
            structured_client=ApplicationJSONClient(),
        )

        with self.assertRaisesRegex(Exception, "Select a target repository"):
            application.resolve_project()

    def test_verified_memory_is_prefetched_before_planning(self) -> None:
        state_dir = self.test_root / "memory-prefetch-state"
        state_dir.mkdir(parents=True)
        client = ApplicationJSONClient()
        application = RepoAgentApplication(
            RepoAgentApplicationConfig(
                state_dir=state_dir,
                skills_root=PROJECT_ROOT / "skills",
            ),
            structured_client=client,
        )
        context = application.resolve_project(repo=self.repo_root)
        with SQLiteMemoryStore(
            state_dir / "memory.sqlite3",
            FeatureHashEmbeddingClient(256),
        ) as store:
            MemoryManager(store).remember_verified_fact(
                context,
                "项目入口由 src/demo/main.py 的 main 函数提供",
                evidence=("src/demo/main.py:1-2",),
                source_id="manual-entry-fact",
                scope="project",
            )

        result = application.explain(
            "请解释项目入口 main 函数",
            repo=self.repo_root,
            thread_id="memory-prefetch",
        )

        self.assertTrue(
            any(item.memory_hits == 1 for item in result.context_prefetches)
        )
        planner_request = next(
            item for item in client.requests if item.schema_name == "execution_plan"
        )
        self.assertIn(
            "项目入口由 src/demo/main.py 的 main 函数提供",
            planner_request.messages[-1].content,
        )

    def test_fix_creates_verified_proposal_and_only_apply_changes_source(self) -> None:
        client = ApplicationJSONClient()
        runner = PassingProcessRunner()
        service = RepoAgentMaintenanceService(
            RepoAgentApplicationConfig(
                state_dir=self.test_root / "state",
                skills_root=PROJECT_ROOT / "skills",
            ),
            structured_client=client,
            process_runner=runner,
        )
        original = (self.repo_root / "src" / "demo" / "main.py").read_text(
            encoding="utf-8"
        )

        proposal, artifact_path, _ = service.propose(
            "把入口函数返回值改为 fixed",
            repo=self.repo_root,
            allow_code_execution=True,
            thread_id="fix-entry",
        )

        self.assertTrue(proposal.evaluation.passed)
        self.assertTrue(artifact_path.is_file())
        self.assertEqual(len(runner.calls), 2)
        self.assertEqual(
            (self.repo_root / "src" / "demo" / "main.py").read_text(
                encoding="utf-8"
            ),
            original,
        )
        with self.assertRaises(CandidatePromotionError):
            service.apply(proposal.proposal_id, approved=False)

        result = service.apply(proposal.proposal_id, approved=True)

        self.assertEqual(result.changed_files, ("src/demo/main.py",))
        self.assertIn(
            "return 'fixed'",
            (self.repo_root / "src" / "demo" / "main.py").read_text(
                encoding="utf-8"
            ),
        )


if __name__ == "__main__":
    unittest.main()
