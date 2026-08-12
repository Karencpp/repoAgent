from __future__ import annotations

from collections import deque
import json
from pathlib import Path
import shutil
import sys
import unittest
from typing import Any, Mapping
from uuid import uuid4

from pydantic import BaseModel, ConfigDict


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
TEST_TEMP_ROOT = PROJECT_ROOT / ".skill-test-tmp"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from repo_agent.context_engineering import ContextBuilder, skill_packet, task_packet
from repo_agent.llm import StructuredDecisionClient, StructuredJSONRequest
from repo_agent.react import (
    ModelRequest,
    ReActExecutor,
    ScriptedDecisionClient,
    StructuredDecisionModel,
)
from repo_agent.skills import (
    SkillActivationError,
    SkillAwareReActExecutor,
    SkillCatalog,
    SkillChangedError,
    SkillManager,
    SkillResourceError,
    SkillRouter,
    register_skill_script_tools,
    skill_script_scope,
)
from repo_agent.tools import ToolDefinition, ToolRegistry, ToolResult


class EmptyArguments(BaseModel):
    """测试工具不接收参数。"""

    model_config = ConfigDict(extra="forbid")


class RecordingJSONClient:
    """记录结构化模型请求并返回预设对象。"""

    def __init__(self, responses: list[Mapping[str, Any]]) -> None:
        self.responses = deque(responses)
        self.requests: list[StructuredJSONRequest] = []

    def generate_json(self, request: StructuredJSONRequest) -> Mapping[str, Any]:
        self.requests.append(request)
        return self.responses.popleft()


def build_registry(*names: str) -> ToolRegistry:
    """构造只记录工具名的轻量注册表。"""

    registry = ToolRegistry()
    for name in names:
        registry.register(
            ToolDefinition(
                name=name,
                description=f"测试工具 {name}",
                access="read",
                executes_project_code=False,
                requires_explicit_authorization=False,
            ),
            EmptyArguments,
            lambda _arguments, tool_name=name: ToolResult.success(tool_name),
        )
    return registry


def write_skill(
    skills_root: Path,
    name: str,
    *,
    description: str = "诊断 pytest 测试失败时使用。",
    version: str = "1.0.0",
    allowed_tools: tuple[str, ...] = ("read_file", "run_tests"),
    required_tools: tuple[str, ...] = ("read_file",),
    modes: tuple[str, ...] = ("diagnose", "fix"),
    tags: tuple[str, ...] = ("pytest",),
    triggers: tuple[str, ...] = ("测试失败",),
    body: str = "# 诊断规程\n\n先读取证据，再判断根因。",
    resources: tuple[str, ...] = (),
) -> Path:
    """在测试隔离目录生成一个符合约定的 Skill。"""

    skill_root = skills_root / name
    skill_root.mkdir(parents=True, exist_ok=True)
    lines = [
        "---",
        f"name: {name}",
        f"description: {description}",
    ]
    if allowed_tools:
        lines.append(f"allowed-tools: {' '.join(allowed_tools)}")
    lines.extend(
        [
            "metadata:",
            f"  version: {version}",
            "  modes:",
            *(f"    - {mode}" for mode in modes),
            "  tags:",
            *(f"    - {tag}" for tag in tags),
            "  triggers:",
            *(f"    - {trigger}" for trigger in triggers),
            "  required-tools:",
            *(f"    - {tool}" for tool in required_tools),
        ]
    )
    if resources:
        lines.extend(
            [
                "  resources:",
                *(f"    - {resource}" for resource in resources),
            ]
        )
    lines.extend(["---", body, ""])
    skill_file = skill_root / "SKILL.md"
    skill_file.write_text("\n".join(lines), encoding="utf-8")
    return skill_file


class SkillSystemTests(unittest.TestCase):
    def setUp(self) -> None:
        TEST_TEMP_ROOT.mkdir(parents=True, exist_ok=True)
        self.root = TEST_TEMP_ROOT / f"case-{uuid4().hex}"
        self.skills_root = self.root / "trusted-skills"
        self.skills_root.mkdir(parents=True)

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def catalog_with_default_skill(self) -> SkillCatalog:
        """创建包含一个有效测试 Skill 的目录。"""

        write_skill(self.skills_root, "diagnose-tests")
        catalog = SkillCatalog((self.skills_root,))
        catalog.refresh()
        return catalog

    def test_discovery_only_exposes_lightweight_metadata(self) -> None:
        catalog = self.catalog_with_default_skill()

        descriptor = catalog.descriptors()[0]

        self.assertEqual(descriptor.name, "diagnose-tests")
        self.assertEqual(descriptor.version, "1.0.0")
        self.assertFalse(hasattr(descriptor, "instructions"))

    def test_invalid_skill_is_skipped_with_diagnostic(self) -> None:
        write_skill(self.skills_root, "bad-version", version="1.0")

        result = SkillCatalog((self.skills_root,)).refresh()

        self.assertEqual(result.skills, ())
        self.assertEqual(result.diagnostics[0].code, "invalid_skill")
        self.assertIn("SemVer", result.diagnostics[0].message)

    def test_directory_name_must_match_skill_name(self) -> None:
        skill_file = write_skill(self.skills_root, "declared-name")
        mismatched = self.skills_root / "other-directory"
        skill_file.parent.rename(mismatched)

        result = SkillCatalog((self.skills_root,)).refresh()

        self.assertEqual(result.skills, ())
        self.assertIn("父目录名", result.diagnostics[0].message)

    def test_duplicate_name_across_trusted_roots_fails_closed(self) -> None:
        other_root = self.root / "other-trusted-skills"
        write_skill(self.skills_root, "diagnose-tests")
        write_skill(other_root, "diagnose-tests")

        result = SkillCatalog((self.skills_root, other_root)).refresh()

        self.assertEqual(result.skills, ())
        self.assertEqual(
            {item.code for item in result.diagnostics},
            {"duplicate_skill_name"},
        )

    def test_target_repository_skill_is_not_automatically_trusted(self) -> None:
        target_repo = self.root / "target-repo"
        write_skill(target_repo / ".agents" / "skills", "malicious-skill")
        catalog = SkillCatalog((self.skills_root,))

        result = catalog.refresh()

        self.assertEqual(result.skills, ())

    def test_symlink_skill_directory_is_rejected(self) -> None:
        real_root = self.root / "untrusted"
        write_skill(real_root, "linked-skill")
        link = self.skills_root / "linked-skill"
        try:
            link.symlink_to(real_root / "linked-skill", target_is_directory=True)
        except OSError:
            self.skipTest("当前 Windows 环境不允许创建目录符号链接")

        result = SkillCatalog((self.skills_root,)).refresh()

        self.assertEqual(result.skills, ())
        self.assertIn("符号链接", result.diagnostics[0].message)

    def test_router_uses_metadata_without_loading_body(self) -> None:
        catalog = self.catalog_with_default_skill()

        matches = SkillRouter().route(
            "pytest 测试失败，请帮我定位",
            catalog.descriptors(),
            mode="diagnose",
        )

        self.assertEqual(matches[0].skill.name, "diagnose-tests")
        self.assertGreaterEqual(matches[0].score, 10)
        self.assertTrue(any("pytest" in reason for reason in matches[0].reasons))

    def test_router_filters_incompatible_mode(self) -> None:
        write_skill(
            self.skills_root,
            "fix-only",
            modes=("fix",),
            triggers=("重构",),
        )
        catalog = SkillCatalog((self.skills_root,))
        catalog.refresh()

        matches = SkillRouter().route(
            "请重构这段代码",
            catalog.descriptors(),
            mode="diagnose",
        )

        self.assertEqual(matches, ())

    def test_activation_intersects_skill_and_runtime_tools(self) -> None:
        catalog = self.catalog_with_default_skill()
        registry = build_registry("read_file", "run_tests", "dangerous_delete")

        activated = catalog.activate(
            "diagnose-tests",
            registry,
            runtime_allowed_tools=("read_file", "dangerous_delete"),
            mode="diagnose",
        )

        self.assertEqual(activated.effective_tools, ("read_file",))
        self.assertNotIn("dangerous_delete", activated.effective_tools)

    def test_skill_cannot_grant_tool_missing_from_runtime_allowlist(self) -> None:
        catalog = self.catalog_with_default_skill()
        registry = build_registry("read_file", "run_tests")

        activated = catalog.activate(
            "diagnose-tests",
            registry,
            runtime_allowed_tools=("read_file",),
        )

        self.assertEqual(activated.effective_tools, ("read_file",))

    def test_missing_required_tool_blocks_activation(self) -> None:
        catalog = self.catalog_with_default_skill()
        registry = build_registry("run_tests")

        with self.assertRaises(SkillActivationError):
            catalog.activate("diagnose-tests", registry)

    def test_activation_rejects_incompatible_mode(self) -> None:
        write_skill(self.skills_root, "fix-only", modes=("fix",))
        catalog = SkillCatalog((self.skills_root,))
        catalog.refresh()

        with self.assertRaises(SkillActivationError):
            catalog.activate(
                "fix-only",
                build_registry("read_file", "run_tests"),
                mode="diagnose",
            )

    def test_referenced_resource_is_loaded_only_on_demand(self) -> None:
        body = (
            "# 诊断规程\n\n"
            "需要时读取 [分类表](references/classification.md)。"
        )
        write_skill(self.skills_root, "diagnose-tests", body=body)
        reference = (
            self.skills_root
            / "diagnose-tests"
            / "references"
            / "classification.md"
        )
        reference.parent.mkdir(parents=True)
        reference.write_text("断言失败不等于工具失败。", encoding="utf-8")
        catalog = SkillCatalog((self.skills_root,))
        catalog.refresh()
        activated = catalog.activate(
            "diagnose-tests",
            build_registry("read_file", "run_tests"),
        )

        resource = catalog.load_resource(
            activated,
            "references/classification.md",
        )

        self.assertIn("工具失败", resource.content)
        self.assertEqual(len(resource.content_hash), 64)

    def test_undeclared_and_traversal_resources_are_rejected(self) -> None:
        catalog = self.catalog_with_default_skill()
        activated = catalog.activate(
            "diagnose-tests",
            build_registry("read_file", "run_tests"),
        )

        with self.assertRaises(SkillResourceError):
            catalog.load_resource(activated, "references/hidden.md")
        with self.assertRaises(SkillResourceError):
            catalog.load_resource(activated, "../secret.txt")

    def test_snapshot_detects_body_change_during_resume(self) -> None:
        catalog = self.catalog_with_default_skill()
        registry = build_registry("read_file", "run_tests")
        activated = catalog.activate("diagnose-tests", registry)
        skill_file = activated.descriptor.skill_file
        skill_file.write_text(
            skill_file.read_text(encoding="utf-8") + "\n新增步骤。",
            encoding="utf-8",
        )

        with self.assertRaises(SkillChangedError):
            catalog.validate_snapshot(activated.snapshot, registry)

    def test_metadata_change_requires_refresh_before_activation(self) -> None:
        catalog = self.catalog_with_default_skill()
        descriptor = catalog.descriptors()[0]
        text = descriptor.skill_file.read_text(encoding="utf-8")
        descriptor.skill_file.write_text(
            text.replace("version: 1.0.0", "version: 1.0.1"),
            encoding="utf-8",
        )

        with self.assertRaises(SkillChangedError):
            catalog.activate(
                "diagnose-tests",
                build_registry("read_file", "run_tests"),
            )

    def test_skill_packet_enters_trusted_instruction_zone(self) -> None:
        built = ContextBuilder().build(
            (
                task_packet("定位测试失败"),
                skill_packet("diagnose-tests", "先读取证据"),
            )
        )

        self.assertIn("<TRUSTED_INSTRUCTIONS>", built.content)
        self.assertIn('"source": "skill"', built.content)
        self.assertLess(
            built.content.index("<TRUSTED_INSTRUCTIONS>"),
            built.content.index("<USER_REQUEST>"),
        )

    def test_structured_decision_client_receives_skill_as_trusted_context(self) -> None:
        raw_client = RecordingJSONClient(
            [
                {
                    "type": "final_answer",
                    "answer": "完成",
                    "decision_summary": "证据足够",
                }
            ]
        )
        client = StructuredDecisionClient(raw_client)
        client.generate_decision(
            ModelRequest(
                user_goal="诊断失败",
                system_instructions="",
                available_tools=(),
                observations=(),
                remaining_iterations=1,
                remaining_tool_calls=0,
                skill_instructions=("Skill 正文：先分类失败。",),
            )
        )

        content = raw_client.requests[0].messages[1].content
        self.assertIn("<TRUSTED_INSTRUCTIONS>", content)
        self.assertIn("Skill 正文", content)

    def test_skill_aware_react_uses_effective_tools_and_instructions(self) -> None:
        catalog = self.catalog_with_default_skill()
        registry = build_registry("read_file", "run_tests", "dangerous_delete")
        scripted = ScriptedDecisionClient(
            [
                {
                    "type": "tool_call",
                    "tool_name": "read_file",
                    "arguments": {},
                    "decision_summary": "先读取必需证据",
                },
                {
                    "type": "final_answer",
                    "answer": "完成",
                    "decision_summary": "已按规程分析",
                }
            ]
        )
        react = ReActExecutor(StructuredDecisionModel(scripted), registry)
        executor = SkillAwareReActExecutor(
            react,
            SkillManager(catalog, registry),
        )

        result = executor.run(
            "pytest 测试失败",
            skill_name="diagnose-tests",
            mode="diagnose",
            allowed_tools=("read_file", "dangerous_delete"),
            runtime_required_tools=("read_file",),
        )

        request = scripted.requests[0]
        self.assertEqual(result.active_skill.descriptor.name, "diagnose-tests")
        self.assertEqual(
            [tool.name for tool in request.available_tools],
            ["read_file"],
        )
        self.assertIn("诊断规程", request.skill_instructions[0])
        self.assertEqual(request.remaining_required_tools, ("read_file",))
        self.assertEqual(result.react_result.status, "completed")


class BundledSkillPackageTests(unittest.TestCase):
    """验证仓库内置的两个 Skill v2 能力包可以真实运行。"""

    def setUp(self) -> None:
        self.catalog = SkillCatalog((PROJECT_ROOT / "skills",))
        result = self.catalog.refresh()
        self.assertEqual(result.diagnostics, ())
        self.registry = build_registry(
            "list_files",
            "search_code",
            "read_file_range",
            "inspect_python",
            "run_pytest",
        )
        register_skill_script_tools(self.catalog, self.registry)

    def test_two_bundled_skills_are_complete_v2_packages(self) -> None:
        descriptors = {item.name: item for item in self.catalog.descriptors()}

        self.assertEqual(
            set(descriptors),
            {"diagnose-pytest-failure", "safe-python-refactor"},
        )
        expected_versions = {
            "diagnose-pytest-failure": "2.1.0",
            "safe-python-refactor": "2.0.0",
        }
        for descriptor in descriptors.values():
            self.assertEqual(descriptor.package_format, "v2")
            self.assertEqual(descriptor.version, expected_versions[descriptor.name])
            self.assertTrue(descriptor.instruction_resources)
            self.assertTrue(descriptor.assets)
            self.assertTrue(descriptor.tests)
            self.assertEqual(len(descriptor.scripts), 1)

    def test_activation_loads_declared_instruction_resources(self) -> None:
        activated = self.catalog.activate(
            "safe-python-refactor",
            self.registry,
            mode="fix",
        )

        rendered = activated.render_instructions()
        self.assertIn("重构检查表", rendered)
        self.assertIn("Python 兼容性规则", rendered)
        self.assertIn("analyze_python_api_change", rendered)
        self.assertEqual(len(activated.loaded_resources), 2)

    def test_script_tool_is_denied_without_matching_skill_scope(self) -> None:
        result = self.registry.dispatch(
            "classify_pytest_failure",
            {
                "stdout": "1 passed",
                "stderr": "",
                "exit_code": 0,
                "timed_out": False,
            },
        )

        self.assertFalse(result.ok)
        self.assertIn("激活后调用", result.error.message)

    def test_script_input_is_validated_before_execution(self) -> None:
        with skill_script_scope("diagnose-pytest-failure"):
            result = self.registry.dispatch(
                "classify_pytest_failure",
                {"stdout": "缺少必填字段"},
            )

        self.assertFalse(result.ok)
        self.assertIn("参数校验失败", result.error.message)

    def test_diagnosis_package_cases_all_pass(self) -> None:
        descriptor = self.catalog.get("diagnose-pytest-failure")
        cases = json.loads(
            (descriptor.skill_root / descriptor.tests[0]).read_text(
                encoding="utf-8"
            )
        )

        with skill_script_scope(descriptor.name):
            for case in cases:
                with self.subTest(case=case["name"]):
                    result = self.registry.dispatch(
                        "classify_pytest_failure",
                        case["input"],
                    )
                    self.assertTrue(result.ok, result.error)
                    self.assertEqual(
                        result.data["category"],
                        case["expected_category"],
                    )

    def test_refactor_package_cases_all_pass(self) -> None:
        descriptor = self.catalog.get("safe-python-refactor")
        cases = json.loads(
            (descriptor.skill_root / descriptor.tests[0]).read_text(
                encoding="utf-8"
            )
        )

        with skill_script_scope(descriptor.name):
            for case in cases:
                with self.subTest(case=case["name"]):
                    result = self.registry.dispatch(
                        "analyze_python_api_change",
                        case["input"],
                    )
                    self.assertTrue(result.ok, result.error)
                    self.assertEqual(
                        result.data["risk_level"],
                        case["expected_risk_level"],
                    )

    def test_snapshot_covers_declared_package_files(self) -> None:
        temp_root = TEST_TEMP_ROOT / f"package-{uuid4().hex}"
        copied_root = temp_root / "safe-python-refactor"
        shutil.copytree(
            PROJECT_ROOT / "skills" / "safe-python-refactor",
            copied_root,
        )
        try:
            catalog = SkillCatalog((temp_root,))
            catalog.refresh()
            registry = build_registry(
                "list_files",
                "search_code",
                "read_file_range",
                "inspect_python",
                "run_pytest",
            )
            register_skill_script_tools(catalog, registry)
            activated = catalog.activate("safe-python-refactor", registry)
            reference = copied_root / "references" / "compatibility-rules.md"
            reference.write_text(
                reference.read_text(encoding="utf-8") + "\n新增规则。\n",
                encoding="utf-8",
            )

            with self.assertRaises(SkillChangedError):
                catalog.validate_snapshot(activated.snapshot, registry)
        finally:
            shutil.rmtree(temp_root, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
