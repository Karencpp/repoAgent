"""编译、目标测试、回归测试和 diff 范围的客观 Evaluator。"""

from __future__ import annotations

import difflib
from pathlib import Path
import time

from repo_agent.tools import LocalRepositoryTools, PythonRuntime, RunPytestInput
from repo_agent.tools.process import ProcessRunner
from repo_agent.workflow import EvaluationRequest, EvaluationResult

from .models import (
    CandidateEvaluationConfig,
    CandidateEvaluationReport,
    ValidationCheck,
)
from .workspace import CandidateWorkspace, CandidateWorkspaceError


def _render_workspace_diff(workspace: CandidateWorkspace) -> str:
    """根据基线和当前副本生成完整 unified diff。"""

    baseline = workspace.baseline
    current = workspace.current_files()
    parts: list[str] = []
    for path in sorted(set(baseline) | set(current)):
        before_bytes = baseline.get(path, b"")
        after_bytes = current.get(path, b"")
        if before_bytes == after_bytes:
            continue
        try:
            before = before_bytes.decode("utf-8")
            after = after_bytes.decode("utf-8")
        except UnicodeDecodeError:
            parts.append(f"二进制文件发生变化：{path}")
            continue
        parts.append(
            "\n".join(
                difflib.unified_diff(
                    before.splitlines(),
                    after.splitlines(),
                    fromfile=f"a/{path}",
                    tofile=f"b/{path}",
                    lineterm="",
                )
            )
        )
    return "\n".join(part for part in parts if part)


class ObjectiveCandidateEvaluator:
    """在工作副本上执行确定性验证，并实现 Workflow EvaluatorPort。"""

    def __init__(
        self,
        workspace: CandidateWorkspace,
        config: CandidateEvaluationConfig,
        *,
        process_runner: ProcessRunner | None = None,
        python_runtime: PythonRuntime | None = None,
    ) -> None:
        self.workspace = workspace
        self.config = config
        self.process_runner = process_runner
        self.python_runtime = python_runtime
        self.reports: list[CandidateEvaluationReport] = []

    def _scope_check(
        self,
        changed_files: tuple[str, ...],
        diff_line_count: int,
    ) -> ValidationCheck:
        """检查变更数量、允许路径和 diff 行数。"""

        expected = set(self.config.expected_changed_files)
        unexpected = sorted(set(changed_files) - expected)
        missing = sorted(expected - set(changed_files))
        issues: list[str] = []
        if not changed_files:
            issues.append("候选副本没有任何内容变化")
        if len(changed_files) > self.config.max_changed_files:
            issues.append(
                f"变更文件数 {len(changed_files)} 超过上限 {self.config.max_changed_files}"
            )
        if unexpected:
            issues.append(f"出现未授权变更：{', '.join(unexpected)}")
        if missing:
            issues.append(f"预期文件没有变化：{', '.join(missing)}")
        if diff_line_count > self.config.max_diff_lines:
            issues.append(
                f"diff 行数 {diff_line_count} 超过上限 {self.config.max_diff_lines}"
            )
        if issues:
            return ValidationCheck(
                name="change_scope",
                status="failed",
                summary="；".join(issues),
            )
        return ValidationCheck(
            name="change_scope",
            status="passed",
            summary=f"{len(changed_files)} 个变更文件均在授权范围内",
        )

    def _compile_check(self) -> ValidationCheck:
        """编译所有 Python 文本但不 import 或执行模块。"""

        started_at = time.perf_counter()
        errors: list[str] = []
        compiled = 0
        for path in sorted(self.workspace.worktree_root.rglob("*.py")):
            if any(part in {".venv", "venv", "__pycache__"} for part in path.parts):
                continue
            try:
                source = path.read_text(encoding="utf-8")
                compile(source, str(path), "exec")
                compiled += 1
            except (OSError, UnicodeError, SyntaxError) as exc:
                relative = path.relative_to(self.workspace.worktree_root).as_posix()
                errors.append(f"{relative}: {type(exc).__name__}: {exc}")
        duration_ms = int((time.perf_counter() - started_at) * 1000)
        if errors:
            return ValidationCheck(
                name="python_compile",
                status="failed",
                summary="\n".join(errors)[:5_000],
                duration_ms=duration_ms,
            )
        return ValidationCheck(
            name="python_compile",
            status="passed",
            summary=f"成功编译 {compiled} 个 Python 文件",
            duration_ms=duration_ms,
        )

    def _pytest_check(
        self,
        name: str,
        targets: tuple[str, ...],
    ) -> ValidationCheck:
        """在工作副本中运行受限 pytest 并保留完整进程观察。"""

        if not targets:
            return ValidationCheck(
                name=name,
                status="skipped",
                summary="没有配置测试目标",
            )
        tools = LocalRepositoryTools(
            self.workspace.context,
            process_runner=self.process_runner,
            allow_code_execution=self.config.allow_code_execution,
            python_runtime=self.python_runtime,
        )
        result = tools.run_pytest(
            RunPytestInput(
                targets=targets,
                timeout_seconds=self.config.test_timeout_seconds,
                output_limit=self.config.output_limit,
            )
        )
        process = result.data
        if not result.ok:
            return ValidationCheck(
                name=name,
                status="error",
                summary=result.error.message,
                duration_ms=process.duration_ms if process is not None else 0,
                command=process.command if process is not None else (),
                exit_code=process.exit_code if process is not None else None,
                stdout=process.stdout if process is not None else "",
                stderr=process.stderr if process is not None else "",
            )
        passed = bool(result.metadata.get("tests_passed"))
        return ValidationCheck(
            name=name,
            status="passed" if passed else "failed",
            summary="pytest 通过" if passed else "pytest 返回失败结果",
            duration_ms=process.duration_ms,
            command=process.command,
            exit_code=process.exit_code,
            stdout=process.stdout,
            stderr=process.stderr,
        )

    def evaluate_candidate(self) -> CandidateEvaluationReport:
        """按范围、编译、目标测试、回归测试顺序执行验证。"""

        try:
            changed_files = self.workspace.changed_files()
            unified_diff = _render_workspace_diff(self.workspace)
        except (CandidateWorkspaceError, OSError) as exc:
            report = CandidateEvaluationReport(
                passed=False,
                changed_files=(),
                checks=(
                    ValidationCheck(
                        name="change_scope",
                        status="error",
                        summary=f"工作副本无法安全扫描：{exc}",
                    ),
                    ValidationCheck(
                        name="python_compile",
                        status="skipped",
                        summary="工作副本扫描失败，跳过编译",
                    ),
                    ValidationCheck(
                        name="target_tests",
                        status="skipped",
                        summary="工作副本扫描失败，拒绝执行项目代码",
                    ),
                    ValidationCheck(
                        name="regression_tests",
                        status="skipped",
                        summary="工作副本扫描失败，拒绝执行项目代码",
                    ),
                ),
                unified_diff="",
                summary="候选修改未通过：change_scope",
            )
            self.reports.append(report)
            return report
        diff_line_count = len(unified_diff.splitlines())
        checks: list[ValidationCheck] = [
            self._scope_check(changed_files, diff_line_count)
        ]
        if checks[-1].status != "passed":
            checks.extend(
                [
                    ValidationCheck(
                        name="python_compile",
                        status="skipped",
                        summary="变更范围校验失败，拒绝继续验证",
                    ),
                    ValidationCheck(
                        name="target_tests",
                        status="skipped",
                        summary="变更范围校验失败，拒绝执行项目代码",
                    ),
                    ValidationCheck(
                        name="regression_tests",
                        status="skipped",
                        summary="变更范围校验失败，拒绝执行项目代码",
                    ),
                ]
            )
        else:
            compile_check = self._compile_check()
            checks.append(compile_check)
            if compile_check.status != "passed":
                checks.extend(
                    [
                        ValidationCheck(
                            name="target_tests",
                            status="skipped",
                            summary="编译失败，跳过目标测试",
                        ),
                        ValidationCheck(
                            name="regression_tests",
                            status="skipped",
                            summary="编译失败，跳过回归测试",
                        ),
                    ]
                )
            elif not self.config.allow_code_execution:
                checks.extend(
                    [
                        ValidationCheck(
                            name="target_tests",
                            status="error",
                            summary="未获得执行目标项目代码的显式授权",
                        ),
                        ValidationCheck(
                            name="regression_tests",
                            status="skipped",
                            summary="目标测试未授权，跳过回归测试",
                        ),
                    ]
                )
            else:
                target_check = self._pytest_check(
                    "target_tests",
                    self.config.target_tests,
                )
                checks.append(target_check)
                if target_check.status == "passed":
                    checks.append(
                        self._pytest_check(
                            "regression_tests",
                            self.config.regression_targets,
                        )
                    )
                else:
                    checks.append(
                        ValidationCheck(
                            name="regression_tests",
                            status="skipped",
                            summary="目标测试未通过，跳过回归测试",
                        )
                    )

        all_passed = all(check.status == "passed" for check in checks)
        failed_names = [
            check.name for check in checks if check.status in {"failed", "error"}
        ]
        summary = (
            "候选修改通过全部客观验证"
            if all_passed
            else f"候选修改未通过：{', '.join(failed_names)}"
        )
        report = CandidateEvaluationReport(
            passed=all_passed,
            changed_files=changed_files,
            checks=tuple(checks),
            unified_diff=unified_diff,
            summary=summary,
        )
        self.reports.append(report)
        return report

    def evaluate(self, request: EvaluationRequest) -> EvaluationResult:
        """把客观候选评估转换成 LangGraph 的任务级评估结果。"""

        if request.project_id != self.workspace.source_context.project_id:
            return EvaluationResult(
                passed=False,
                summary="Evaluator 工作副本与 Graph 项目身份不一致",
                issues=("candidate_workspace_project_mismatch",),
            )
        if request.repo_revision != self.workspace.source_context.revision:
            return EvaluationResult(
                passed=False,
                summary="Evaluator 工作副本基线版本与 Graph revision 不一致",
                issues=("candidate_workspace_revision_mismatch",),
            )
        report = self.evaluate_candidate()
        issues = tuple(
            f"{check.name}: {check.summary}"
            for check in report.checks
            if check.status in {"failed", "error"}
        )
        evidence = tuple(
            [f"changed_file:{path}" for path in report.changed_files]
            + [
                f"check:{check.name}:{check.status}"
                for check in report.checks
            ]
        )
        return EvaluationResult(
            passed=report.passed,
            summary=report.summary,
            issues=issues,
            evidence=evidence,
        )
