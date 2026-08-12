from __future__ import annotations

from pathlib import Path
import os
import shutil
import sys
import time
import unittest
from uuid import uuid4


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
TEST_TEMP_ROOT = PROJECT_ROOT / ".tool-test-tmp"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from repo_agent.projects import ProjectContextResolver, ProjectRegistry
from repo_agent.tools import (
    InspectPythonInput,
    ListFilesInput,
    LocalRepositoryTools,
    ProcessResult,
    ReadFileRangeInput,
    RunPytestInput,
    SearchCodeInput,
    SecureSubprocessRunner,
    ToolErrorKind,
)


class CapturingProcessRunner:
    """记录调用参数并返回预设结果的测试替身。"""

    def __init__(self, result: ProcessResult) -> None:
        self.result = result
        self.calls: list[dict[str, object]] = []

    def run(
        self,
        command: list[str] | tuple[str, ...],
        *,
        cwd: Path,
        timeout_seconds: float,
        output_limit: int,
    ) -> ProcessResult:
        self.calls.append(
            {
                "command": tuple(command),
                "cwd": cwd,
                "timeout_seconds": timeout_seconds,
                "output_limit": output_limit,
            }
        )
        return self.result


class RepositoryToolsTests(unittest.TestCase):
    def setUp(self) -> None:
        TEST_TEMP_ROOT.mkdir(parents=True, exist_ok=True)
        self.root = TEST_TEMP_ROOT / f"case-{uuid4().hex}"
        self.repo = self.root / "target-repo"
        self.repo.mkdir(parents=True)
        (self.repo / "src").mkdir()
        (self.repo / "tests").mkdir()
        (self.repo / ".venv").mkdir()
        (self.repo / "README.md").write_text(
            "# 示例项目\n\n金额统一使用 Decimal。\n", encoding="utf-8"
        )
        (self.repo / "src" / "billing.py").write_text(
            """import decimal
from pathlib import Path


class BillingService:
    \"\"\"账单服务。\"\"\"

    def calculate_total(self, amount: str) -> decimal.Decimal:
        \"\"\"计算订单总额。\"\"\"
        return decimal.Decimal(amount)

    async def save(self) -> None:
        return None


def helper(value: int) -> int:
    return value + 1
""",
            encoding="utf-8",
        )
        (self.repo / "tests" / "test_billing.py").write_text(
            """from src.billing import BillingService


def test_total():
    assert str(BillingService().calculate_total("1.20")) == "1.20"
""",
            encoding="utf-8",
        )
        (self.repo / ".venv" / "secret.py").write_text(
            "SHOULD_NOT_BE_VISIBLE = True\n", encoding="utf-8"
        )
        registry = ProjectRegistry(self.root / "state" / "projects.json")
        self.context = ProjectContextResolver(registry).resolve(repo=self.repo)
        self.tools = LocalRepositoryTools(self.context)

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def test_list_files_respects_ignored_directories_and_depth(self) -> None:
        result = self.tools.list_files(ListFilesInput(max_depth=2))

        self.assertTrue(result.ok)
        paths = {entry.path for entry in result.data or ()}
        self.assertIn("README.md", paths)
        self.assertIn("src/billing.py", paths)
        self.assertIn("tests/test_billing.py", paths)
        self.assertNotIn(".venv", paths)
        self.assertNotIn(".venv/secret.py", paths)

    def test_list_files_supports_glob_and_result_limit(self) -> None:
        result = self.tools.list_files(
            ListFilesInput(max_depth=3, max_results=1, file_glob="*.py")
        )

        self.assertTrue(result.ok)
        self.assertEqual(len(result.data or ()), 1)
        self.assertTrue(result.metadata["result_limit_reached"])
        self.assertTrue((result.data or ())[0].path.endswith(".py"))

    def test_search_code_returns_path_line_and_column(self) -> None:
        result = self.tools.search_code(
            SearchCodeInput(query="billingservice", file_glob="*.py")
        )

        self.assertTrue(result.ok)
        matches = result.data or ()
        self.assertGreaterEqual(len(matches), 2)
        self.assertEqual(matches[0].path, "src/billing.py")
        self.assertGreater(matches[0].line_number, 0)
        self.assertGreater(matches[0].column_number, 0)
        self.assertGreaterEqual(result.metadata["scanned_files"], 2)

    def test_search_code_rejects_empty_query_and_unsafe_glob(self) -> None:
        empty = self.tools.search_code(SearchCodeInput(query=""))
        unsafe = self.tools.search_code(
            SearchCodeInput(query="Billing", file_glob="../*.py")
        )

        self.assertFalse(empty.ok)
        self.assertEqual(empty.error.kind, ToolErrorKind.INVALID_ARGUMENT)
        self.assertFalse(unsafe.ok)
        self.assertEqual(unsafe.error.kind, ToolErrorKind.INVALID_ARGUMENT)

    def test_read_file_range_returns_local_slice_and_total_lines(self) -> None:
        result = self.tools.read_file_range(
            ReadFileRangeInput(path="src/billing.py", start_line=4, end_line=8)
        )

        self.assertTrue(result.ok)
        self.assertEqual(result.data.path, "src/billing.py")
        self.assertEqual(result.data.start_line, 4)
        self.assertIn("BillingService", result.data.content)
        self.assertGreater(result.data.total_lines, 8)

    def test_read_file_range_marks_character_truncation(self) -> None:
        result = self.tools.read_file_range(
            ReadFileRangeInput(
                path="src/billing.py", start_line=1, end_line=20, max_chars=30
            )
        )

        self.assertTrue(result.ok)
        self.assertTrue(result.data.truncated)
        self.assertLessEqual(len(result.data.content), 30)

    def test_read_file_range_blocks_repository_escape(self) -> None:
        outside = self.root / "outside.txt"
        outside.write_text("secret", encoding="utf-8")
        result = self.tools.read_file_range(
            ReadFileRangeInput(path=str(outside), start_line=1, end_line=1)
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.error.kind, ToolErrorKind.PERMISSION_DENIED)

    def test_inspect_python_returns_imports_and_qualified_definitions(self) -> None:
        result = self.tools.inspect_python(
            InspectPythonInput(path="src/billing.py")
        )

        self.assertTrue(result.ok)
        inspection = result.data
        modules = {item.module for item in inspection.imports}
        qualified_names = {item.qualified_name for item in inspection.definitions}
        self.assertIn("pathlib", modules)
        self.assertIn("BillingService", qualified_names)
        self.assertIn("BillingService.calculate_total", qualified_names)
        self.assertIn("BillingService.save", qualified_names)
        self.assertIn("helper", qualified_names)

    def test_inspect_python_supports_symbol_filter(self) -> None:
        result = self.tools.inspect_python(
            InspectPythonInput(
                path="src/billing.py", symbol="BillingService.calculate_total"
            )
        )

        self.assertTrue(result.ok)
        self.assertEqual(len(result.data.definitions), 1)
        self.assertEqual(
            result.data.definitions[0].qualified_name,
            "BillingService.calculate_total",
        )

    def test_inspect_python_returns_structured_parse_error(self) -> None:
        (self.repo / "src" / "broken.py").write_text(
            "def broken(:\n    pass\n", encoding="utf-8"
        )
        result = self.tools.inspect_python(
            InspectPythonInput(path="src/broken.py")
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.error.kind, ToolErrorKind.PARSE_ERROR)
        self.assertEqual(result.error.details["line_number"], 1)

    def test_pytest_nonzero_exit_is_observation_not_tool_error(self) -> None:
        process_result = ProcessResult(
            command=("python", "-m", "pytest"),
            exit_code=1,
            stdout="1 failed",
            stderr="",
            duration_ms=20,
            timed_out=False,
            output_truncated=False,
        )
        runner = CapturingProcessRunner(process_result)
        tools = LocalRepositoryTools(
            self.context,
            process_runner=runner,
            allow_code_execution=True,
        )
        result = tools.run_pytest(
            RunPytestInput(
                targets=("tests/test_billing.py::test_total",),
                keyword="total",
            )
        )

        self.assertTrue(result.ok)
        self.assertFalse(result.metadata["tests_passed"])
        call = runner.calls[0]
        command = call["command"]
        self.assertIn("tests/test_billing.py::test_total", command)
        self.assertEqual(call["cwd"], self.repo.resolve())
        self.assertIn("-k", command)

    def test_pytest_prefers_repository_virtual_environment(self) -> None:
        executable = (
            self.repo / ".venv" / "Scripts" / "python.exe"
            if os.name == "nt"
            else self.repo / ".venv" / "bin" / "python"
        )
        executable.parent.mkdir(parents=True, exist_ok=True)
        executable.write_bytes(b"")
        process_result = ProcessResult(
            command=(),
            exit_code=0,
            stdout="1 passed",
            stderr="",
            duration_ms=1,
            timed_out=False,
            output_truncated=False,
        )
        runner = CapturingProcessRunner(process_result)
        tools = LocalRepositoryTools(
            self.context,
            process_runner=runner,
            allow_code_execution=True,
        )

        result = tools.run_pytest(RunPytestInput(targets=("tests",)))

        self.assertTrue(result.ok)
        self.assertEqual(Path(runner.calls[0]["command"][0]), executable.resolve())
        self.assertEqual(result.metadata["python_runtime_source"], "repository_venv")

    def test_pytest_rejects_target_outside_repository(self) -> None:
        process_result = ProcessResult(
            command=(),
            exit_code=0,
            stdout="",
            stderr="",
            duration_ms=0,
            timed_out=False,
            output_truncated=False,
        )
        runner = CapturingProcessRunner(process_result)
        tools = LocalRepositoryTools(
            self.context,
            process_runner=runner,
            allow_code_execution=True,
        )
        outside = self.root / "outside_test.py"
        outside.write_text("", encoding="utf-8")
        result = tools.run_pytest(RunPytestInput(targets=(str(outside),)))

        self.assertFalse(result.ok)
        self.assertEqual(result.error.kind, ToolErrorKind.PERMISSION_DENIED)
        self.assertEqual(runner.calls, [])

    def test_pytest_timeout_is_retryable_tool_error_with_partial_data(self) -> None:
        process_result = ProcessResult(
            command=("python", "-m", "pytest"),
            exit_code=None,
            stdout="collecting...",
            stderr="",
            duration_ms=100,
            timed_out=True,
            output_truncated=False,
        )
        runner = CapturingProcessRunner(process_result)
        tools = LocalRepositoryTools(
            self.context,
            process_runner=runner,
            allow_code_execution=True,
        )
        result = tools.run_pytest(RunPytestInput())

        self.assertFalse(result.ok)
        self.assertEqual(result.error.kind, ToolErrorKind.TIMEOUT)
        self.assertTrue(result.error.retryable)
        self.assertEqual(result.data.stdout, "collecting...")

    def test_pytest_requires_explicit_code_execution_authorization(self) -> None:
        process_result = ProcessResult(
            command=(),
            exit_code=0,
            stdout="",
            stderr="",
            duration_ms=0,
            timed_out=False,
            output_truncated=False,
        )
        runner = CapturingProcessRunner(process_result)
        tools = LocalRepositoryTools(self.context, process_runner=runner)
        result = tools.run_pytest(RunPytestInput())

        self.assertFalse(result.ok)
        self.assertEqual(result.error.kind, ToolErrorKind.PERMISSION_DENIED)
        self.assertEqual(runner.calls, [])

    def test_secure_process_runner_truncates_output(self) -> None:
        runner = SecureSubprocessRunner()
        result = runner.run(
            [sys.executable, "-c", "print('A' * 5000)"],
            cwd=self.repo,
            timeout_seconds=5,
            output_limit=1_000,
        )

        self.assertEqual(result.exit_code, 0)
        self.assertTrue(result.output_truncated)
        self.assertIn("<输出已截断>", result.stdout)

    def test_secure_process_runner_reports_timeout(self) -> None:
        runner = SecureSubprocessRunner()
        started_at = time.perf_counter()
        result = runner.run(
            [sys.executable, "-c", "import time; time.sleep(2)"],
            cwd=self.repo,
            timeout_seconds=0.1,
            output_limit=1_000,
        )

        self.assertTrue(result.timed_out)
        self.assertIsNone(result.exit_code)
        self.assertLess(time.perf_counter() - started_at, 1.5)


if __name__ == "__main__":
    unittest.main()
