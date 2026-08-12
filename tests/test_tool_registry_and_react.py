from __future__ import annotations

from pathlib import Path
import shutil
import sys
import unittest
from uuid import uuid4


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
TEST_TEMP_ROOT = PROJECT_ROOT / ".react-test-tmp"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from repo_agent.projects import ProjectContextResolver, ProjectRegistry
from repo_agent.react import (
    ModelDecisionError,
    ModelRequest,
    ReActConfig,
    ReActExecutor,
    ScriptedDecisionClient,
    StructuredDecisionModel,
    ToolCallDecision,
)
from repo_agent.tools import (
    LocalRepositoryTools,
    ToolErrorKind,
    build_repository_tool_registry,
)


def tool_call(
    name: str,
    arguments: dict[str, object],
    summary: str = "需要读取仓库证据",
) -> dict[str, object]:
    """构造测试使用的工具决策。"""

    return {
        "type": "tool_call",
        "tool_name": name,
        "arguments": arguments,
        "decision_summary": summary,
    }


def final_answer(answer: str = "已经得到结论") -> dict[str, object]:
    """构造测试使用的最终回答决策。"""

    return {
        "type": "final_answer",
        "answer": answer,
        "decision_summary": "现有证据足够回答",
    }


class ToolRegistryAndReActTests(unittest.TestCase):
    def setUp(self) -> None:
        TEST_TEMP_ROOT.mkdir(parents=True, exist_ok=True)
        self.root = TEST_TEMP_ROOT / f"case-{uuid4().hex}"
        self.repo = self.root / "target-repo"
        (self.repo / "src").mkdir(parents=True)
        (self.repo / "src" / "billing.py").write_text(
            "class BillingService:\n    pass\n",
            encoding="utf-8",
        )
        project_registry = ProjectRegistry(self.root / "state" / "projects.json")
        context = ProjectContextResolver(project_registry).resolve(repo=self.repo)
        self.registry = build_repository_tool_registry(LocalRepositoryTools(context))

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def build_executor(
        self,
        responses: list[dict[str, object] | Exception],
        *,
        config: ReActConfig | None = None,
    ) -> tuple[ReActExecutor, ScriptedDecisionClient]:
        """创建使用确定性脚本模型的执行器。"""

        client = ScriptedDecisionClient(responses)
        executor = ReActExecutor(
            StructuredDecisionModel(client),
            self.registry,
            config=config,
        )
        return executor, client

    def test_registry_exposes_json_schema_and_risk_metadata(self) -> None:
        definitions = self.registry.model_tools()

        self.assertEqual(len(definitions), 5)
        search = next(item for item in definitions if item.name == "search_code")
        pytest_tool = next(item for item in definitions if item.name == "run_pytest")
        self.assertEqual(search.input_schema["additionalProperties"], False)
        self.assertIn("query", search.input_schema["required"])
        self.assertEqual(pytest_tool.access, "execute")
        self.assertTrue(pytest_tool.executes_project_code)
        self.assertTrue(pytest_tool.requires_explicit_authorization)

    def test_registry_filters_visible_and_callable_tools(self) -> None:
        definitions = self.registry.model_tools(("search_code",))
        denied = self.registry.dispatch(
            "read_file_range",
            {"path": "src/billing.py"},
            allowed_tools=("search_code",),
        )

        self.assertEqual([item.name for item in definitions], ["search_code"])
        self.assertFalse(denied.ok)
        self.assertEqual(denied.error.kind, ToolErrorKind.PERMISSION_DENIED)

    def test_registry_validates_arguments_before_dispatch(self) -> None:
        invalid = self.registry.dispatch(
            "search_code",
            {"query": "   ", "unexpected": True},
        )

        self.assertFalse(invalid.ok)
        self.assertEqual(invalid.error.kind, ToolErrorKind.INVALID_ARGUMENT)
        self.assertGreaterEqual(len(invalid.error.details["errors"]), 1)

    def test_registry_dispatches_valid_arguments(self) -> None:
        result = self.registry.dispatch(
            "search_code",
            {"query": "BillingService", "file_glob": "*.py"},
        )

        self.assertTrue(result.ok)
        self.assertEqual((result.data or ())[0].path, "src/billing.py")

    def test_structured_model_rejects_invalid_decision(self) -> None:
        client = ScriptedDecisionClient(
            [{"type": "tool_call", "tool_name": "search_code"}]
        )
        model = StructuredDecisionModel(client)
        request = ModelRequest(
            user_goal="定位账单服务",
            system_instructions="",
            available_tools=(),
            observations=(),
            remaining_iterations=1,
            remaining_tool_calls=1,
        )

        with self.assertRaises(ModelDecisionError):
            model.decide(request)

    def test_structured_model_retries_schema_invalid_decision(self) -> None:
        client = ScriptedDecisionClient(
            [
                {"type": "tool_call", "tool_name": "search_code"},
                final_answer("第二次输出满足 Schema"),
            ]
        )
        model = StructuredDecisionModel(client, max_validation_attempts=2)
        request = ModelRequest(
            user_goal="定位账单服务",
            system_instructions="",
            available_tools=(),
            observations=(),
            remaining_iterations=1,
            remaining_tool_calls=1,
        )

        decision = model.decide(request)

        self.assertEqual(decision.answer, "第二次输出满足 Schema")
        self.assertEqual(len(client.requests), 2)

    def test_executor_completes_after_tool_observation(self) -> None:
        executor, client = self.build_executor(
            [
                tool_call("search_code", {"query": "BillingService"}),
                final_answer("BillingService 位于 src/billing.py"),
            ]
        )

        result = executor.run("定位 BillingService")

        self.assertEqual(result.status, "completed")
        self.assertEqual(result.tool_calls, 1)
        self.assertEqual(result.iterations, 2)
        self.assertEqual(len(result.events), 1)
        self.assertTrue(result.events[0].result.ok)
        second_request = client.requests[1]
        self.assertEqual(len(second_request.observations), 1)
        self.assertEqual(second_request.observations[0].result["status"], "success")

    def test_required_tool_blocks_early_final_answer(self) -> None:
        executor, client = self.build_executor(
            [
                final_answer("尚未读取证据"),
                tool_call("read_file_range", {"path": "src/billing.py"}),
                final_answer("已读取源码证据"),
            ]
        )

        result = executor.run(
            "读取账单服务",
            allowed_tools=("read_file_range",),
            required_tools=("read_file_range",),
        )

        self.assertEqual(result.status, "completed")
        self.assertEqual(result.tool_calls, 1)
        self.assertEqual(client.requests[0].remaining_required_tools, ("read_file_range",))
        self.assertEqual(client.requests[1].remaining_required_tools, ("read_file_range",))
        self.assertEqual(client.requests[2].remaining_required_tools, ())

    def test_required_tool_must_be_visible(self) -> None:
        executor, _ = self.build_executor([final_answer()])

        with self.assertRaisesRegex(ValueError, "必需工具未暴露"):
            executor.run(
                "读取账单服务",
                allowed_tools=("search_code",),
                required_tools=("read_file_range",),
            )

    def test_required_tool_call_count_blocks_until_satisfied(self) -> None:
        executor, client = self.build_executor(
            [
                tool_call(
                    "read_file_range",
                    {"path": "src/billing.py", "start_line": 1, "end_line": 1},
                ),
                final_answer("只读了一次"),
                tool_call(
                    "read_file_range",
                    {"path": "src/billing.py", "start_line": 2, "end_line": 2},
                ),
                final_answer("已经读取两次"),
            ]
        )

        result = executor.run(
            "分两段读取账单服务",
            allowed_tools=("read_file_range",),
            required_tool_counts={"read_file_range": 2},
        )

        self.assertEqual(result.status, "completed")
        self.assertEqual(result.tool_calls, 2)
        self.assertEqual(
            client.requests[0].remaining_required_tools,
            ("read_file_range", "read_file_range"),
        )
        self.assertEqual(
            client.requests[1].remaining_required_tools,
            ("read_file_range",),
        )
        self.assertTrue(
            client.requests[2].completion_gate_feedback.startswith(
                "上一轮 final_answer 已被运行时拒绝"
            )
        )

    def test_tool_error_becomes_observation_and_can_be_recovered(self) -> None:
        executor, client = self.build_executor(
            [tool_call("missing_tool", {}), final_answer("已改用已有证据回答")]
        )

        result = executor.run("分析仓库")

        self.assertEqual(result.status, "completed")
        self.assertFalse(result.events[0].result.ok)
        observation = client.requests[1].observations[0]
        self.assertEqual(observation.result["error"]["kind"], "not_found")

    def test_consecutive_tool_errors_trigger_stop_condition(self) -> None:
        executor, _ = self.build_executor(
            [tool_call("missing_one", {}), tool_call("missing_two", {})]
        )

        result = executor.run("分析仓库")

        self.assertEqual(result.status, "tool_error_stopped")
        self.assertEqual(result.tool_calls, 2)
        self.assertEqual(len(result.events), 2)

    def test_duplicate_tool_call_is_stopped_before_second_dispatch(self) -> None:
        repeated = tool_call("search_code", {"query": "BillingService"})
        executor, _ = self.build_executor([repeated, repeated])

        result = executor.run("定位服务")

        self.assertEqual(result.status, "duplicate_call_stopped")
        self.assertEqual(result.tool_calls, 1)
        self.assertEqual(len(result.events), 1)

    def test_tool_call_budget_stops_new_dispatch(self) -> None:
        executor, _ = self.build_executor(
            [
                tool_call("search_code", {"query": "BillingService"}),
                tool_call("list_files", {"max_depth": 1}),
            ],
            config=ReActConfig(max_tool_calls=1),
        )

        result = executor.run("分析仓库")

        self.assertEqual(result.status, "budget_exhausted")
        self.assertEqual(result.tool_calls, 1)

    def test_iteration_budget_stops_unfinished_loop(self) -> None:
        executor, _ = self.build_executor(
            [tool_call("search_code", {"query": "BillingService"})],
            config=ReActConfig(max_iterations=1),
        )

        result = executor.run("定位服务")

        self.assertEqual(result.status, "budget_exhausted")
        self.assertEqual(result.iterations, 1)
        self.assertEqual(result.tool_calls, 1)

    def test_invalid_model_output_becomes_model_error(self) -> None:
        executor, _ = self.build_executor(
            [{"type": "tool_call", "tool_name": "search_code"}]
        )

        result = executor.run("定位服务")

        self.assertEqual(result.status, "model_error")
        self.assertEqual(result.tool_calls, 0)

    def test_only_allowed_tools_are_sent_to_model(self) -> None:
        executor, client = self.build_executor([final_answer()])

        result = executor.run("分析仓库", allowed_tools=("inspect_python",))

        self.assertEqual(result.status, "completed")
        self.assertEqual(
            [item.name for item in client.requests[0].available_tools],
            ["inspect_python"],
        )

    def test_script_model_returns_typed_decision(self) -> None:
        client = ScriptedDecisionClient(
            [tool_call("search_code", {"query": "BillingService"})]
        )
        model = StructuredDecisionModel(client)
        request = ModelRequest(
            user_goal="定位服务",
            system_instructions="",
            available_tools=(),
            observations=(),
            remaining_iterations=1,
            remaining_tool_calls=1,
        )

        decision = model.decide(request)

        self.assertIsInstance(decision, ToolCallDecision)
        self.assertEqual(decision.tool_name, "search_code")


if __name__ == "__main__":
    unittest.main()
