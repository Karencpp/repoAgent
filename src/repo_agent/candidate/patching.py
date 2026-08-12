"""带路径沙箱和旧内容哈希前置条件的候选补丁应用器。"""

from __future__ import annotations

import difflib
from pathlib import Path

from repo_agent.projects import InvalidRepositoryError, PathOutsideRepositoryError

from .models import (
    AppliedFileChange,
    CandidatePatch,
    PatchApplicationResult,
)
from .workspace import CandidateWorkspace, sha256_bytes


class CandidatePatchError(RuntimeError):
    """候选补丁校验或写入失败。"""


class CandidatePatchConflictError(CandidatePatchError):
    """文件内容已经不满足补丁的旧哈希前置条件。"""


class CandidatePatchPermissionError(CandidatePatchError):
    """补丁路径或文件类型超出允许范围。"""


class CandidatePatchApplier:
    """只修改候选副本中的已有 UTF-8 文本文件。"""

    def __init__(
        self,
        workspace: CandidateWorkspace,
        *,
        allowed_suffixes: tuple[str, ...] = (
            ".py",
            ".md",
            ".toml",
            ".txt",
            ".json",
            ".yaml",
            ".yml",
        ),
    ) -> None:
        self.workspace = workspace
        self.allowed_suffixes = allowed_suffixes

    @staticmethod
    def _render_file_diff(path: str, before: str, after: str) -> str:
        """生成使用稳定相对路径的 unified diff。"""

        return "\n".join(
            difflib.unified_diff(
                before.splitlines(),
                after.splitlines(),
                fromfile=f"a/{path}",
                tofile=f"b/{path}",
                lineterm="",
            )
        )

    def apply(self, patch: CandidatePatch) -> PatchApplicationResult:
        """先完整校验所有变更，再写入；写入失败时回滚已改文件。"""

        staged: list[
            tuple[Path, bytes | None, bytes | None, str, str, str]
        ] = []
        diff_parts: list[str] = []
        for change in patch.changes:
            try:
                path = self.workspace.context.resolve_repo_path(
                    change.path,
                    must_exist=change.operation != "create",
                )
            except PathOutsideRepositoryError as exc:
                raise CandidatePatchPermissionError(str(exc)) from exc
            except InvalidRepositoryError as exc:
                raise CandidatePatchError(f"补丁目标不存在：{change.path}") from exc
            if path.suffix.casefold() not in self.allowed_suffixes:
                raise CandidatePatchPermissionError(
                    f"补丁文件类型不在白名单内：{change.path}"
                )
            if change.operation == "create":
                if path.exists():
                    raise CandidatePatchConflictError(
                        f"待创建文件已经存在：{change.path}"
                    )
                if not path.parent.is_dir() or path.parent.is_symlink():
                    raise CandidatePatchPermissionError(
                        f"新文件的父目录必须是已有普通目录：{change.path}"
                    )
                before_bytes = None
                before_text = ""
                after_bytes = (change.replacement_content or "").encode("utf-8")
            else:
                if path.is_symlink() or not path.is_file():
                    raise CandidatePatchPermissionError(
                        f"补丁目标不是普通文件：{change.path}"
                    )
                before_bytes = path.read_bytes()
                actual_sha256 = sha256_bytes(before_bytes)
                if actual_sha256 != change.expected_sha256:
                    raise CandidatePatchConflictError(
                        f"文件旧哈希不匹配：{change.path}，期望 {change.expected_sha256}，实际 {actual_sha256}"
                    )
                try:
                    before_text = before_bytes.decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise CandidatePatchError(
                        f"补丁目标不是 UTF-8 文本：{change.path}"
                    ) from exc
                after_bytes = (
                    None
                    if change.operation == "delete"
                    else (change.replacement_content or "").encode("utf-8")
                )
                if before_bytes == after_bytes:
                    raise CandidatePatchError(
                        f"候选修改没有改变文件内容：{change.path}"
                    )
            staged.append(
                (
                    path,
                    before_bytes,
                    after_bytes,
                    change.path,
                    change.reason,
                    change.operation,
                )
            )
            diff_parts.append(
                self._render_file_diff(
                    change.path,
                    before_text,
                    change.replacement_content or "",
                )
            )

        written: list[tuple[Path, bytes | None]] = []
        try:
            for path, before_bytes, after_bytes, _, _, operation in staged:
                if operation == "delete":
                    path.unlink()
                else:
                    path.write_bytes(after_bytes or b"")
                written.append((path, before_bytes))
        except OSError as exc:
            for path, before_bytes in reversed(written):
                try:
                    if before_bytes is None:
                        path.unlink(missing_ok=True)
                    else:
                        path.write_bytes(before_bytes)
                except OSError:
                    pass
            raise CandidatePatchError(f"候选补丁写入失败并已尝试回滚：{exc}") from exc

        applied = tuple(
            AppliedFileChange(
                path=relative,
                operation=operation,
                before_sha256=(
                    sha256_bytes(before_bytes) if before_bytes is not None else None
                ),
                after_sha256=(
                    sha256_bytes(after_bytes) if after_bytes is not None else None
                ),
                reason=reason,
            )
            for _, before_bytes, after_bytes, relative, reason, operation in staged
        )
        return PatchApplicationResult(
            patch_id=patch.patch_id,
            summary=patch.summary,
            changed_files=tuple(change.path for change in applied),
            changes=applied,
            unified_diff="\n".join(part for part in diff_parts if part),
        )
