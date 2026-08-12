"""候选工作副本、补丁和客观评估的结构化模型。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class CandidateModel(BaseModel):
    """候选修改模型的公共严格配置。"""

    model_config = ConfigDict(extra="forbid", frozen=True)


def _validate_relative_path(value: str) -> str:
    """拒绝绝对路径、上级目录和反斜杠歧义。"""

    from pathlib import PurePosixPath

    if "\\" in value:
        raise ValueError("路径必须使用正斜杠")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise ValueError("路径必须是仓库内的规范相对路径")
    if any(part in {"", "."} for part in path.parts):
        raise ValueError("路径不能包含空片段或当前目录片段")
    return value


class CandidateFileChange(CandidateModel):
    """创建、修改或删除一个受控文本文件。"""

    path: str = Field(min_length=1, max_length=1_000)
    operation: Literal["create", "modify", "delete"] = "modify"
    expected_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    replacement_content: str | None = Field(default=None, max_length=2_000_000)
    reason: str = Field(min_length=1, max_length=1_000)

    _safe_path = field_validator("path")(_validate_relative_path)

    @model_validator(mode="after")
    def validate_operation_fields(self) -> "CandidateFileChange":
        """让每种操作只携带必要且无歧义的字段。"""

        if self.operation == "create":
            if self.expected_sha256 is not None or self.replacement_content is None:
                raise ValueError("create 不能提供旧哈希且必须提供新内容")
        elif self.operation == "modify":
            if self.expected_sha256 is None or self.replacement_content is None:
                raise ValueError("modify 必须提供旧哈希和新内容")
        elif self.expected_sha256 is None or self.replacement_content is not None:
            raise ValueError("delete 必须提供旧哈希且不能提供新内容")
        return self


class CandidatePatch(CandidateModel):
    """一次有限、带旧内容前置条件的候选补丁。"""

    patch_id: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9._-]+$")
    summary: str = Field(min_length=1, max_length=2_000)
    changes: tuple[CandidateFileChange, ...] = Field(min_length=1, max_length=20)

    @model_validator(mode="after")
    def validate_unique_paths(self) -> "CandidatePatch":
        """拒绝同一补丁重复覆盖一个文件。"""

        paths = [change.path for change in self.changes]
        if len(paths) != len(set(paths)):
            raise ValueError("同一补丁不能重复修改同一文件")
        return self


class AppliedFileChange(CandidateModel):
    """一个已经写入工作副本的文件变更记录。"""

    path: str
    operation: Literal["create", "modify", "delete"] = "modify"
    before_sha256: str | None
    after_sha256: str | None
    reason: str


class PatchApplicationResult(CandidateModel):
    """候选补丁应用结果和可审计 diff。"""

    patch_id: str
    summary: str
    changed_files: tuple[str, ...]
    changes: tuple[AppliedFileChange, ...]
    unified_diff: str


class ValidationCheck(CandidateModel):
    """一项确定性验证的状态和证据。"""

    name: Literal[
        "change_scope",
        "python_compile",
        "target_tests",
        "regression_tests",
    ]
    status: Literal["passed", "failed", "error", "skipped"]
    summary: str = Field(min_length=1, max_length=5_000)
    duration_ms: int = Field(default=0, ge=0)
    command: tuple[str, ...] = ()
    exit_code: int | None = None
    stdout: str = Field(default="", max_length=100_000)
    stderr: str = Field(default="", max_length=100_000)


class CandidateEvaluationReport(CandidateModel):
    """候选修改的完整客观评估报告。"""

    passed: bool
    changed_files: tuple[str, ...]
    checks: tuple[ValidationCheck, ...]
    unified_diff: str
    summary: str


class CandidateEvaluationConfig(CandidateModel):
    """验证范围、执行授权和资源上限。"""

    expected_changed_files: tuple[str, ...] = Field(min_length=1, max_length=20)
    target_tests: tuple[str, ...] = Field(min_length=1, max_length=20)
    regression_targets: tuple[str, ...] = Field(
        default=("tests",), min_length=1, max_length=20
    )
    allow_code_execution: bool = False
    max_changed_files: int = Field(default=10, ge=1, le=50)
    max_diff_lines: int = Field(default=1_000, ge=1, le=10_000)
    test_timeout_seconds: float = Field(default=60.0, ge=0.1, le=300)
    output_limit: int = Field(default=20_000, ge=1_000, le=100_000)

    @field_validator("expected_changed_files")
    @classmethod
    def validate_expected_paths(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """校验并去除预期路径中的歧义。"""

        if len(value) != len(set(value)):
            raise ValueError("expected_changed_files 不能重复")
        return tuple(_validate_relative_path(path) for path in value)


class CandidatePromotionResult(CandidateModel):
    """候选修改回写真实仓库后的审计结果。"""

    proposal_id: str = Field(min_length=1, max_length=100)
    project_id: str = Field(min_length=1, max_length=200)
    source_revision: str = Field(min_length=1, max_length=500)
    changed_files: tuple[str, ...] = Field(min_length=1, max_length=20)
    promoted_at: str = Field(min_length=1, max_length=100)
