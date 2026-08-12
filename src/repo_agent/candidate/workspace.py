"""不会修改真实目标仓库的候选工作副本。"""

from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import os
from pathlib import Path
import re
import shutil
from types import TracebackType

from repo_agent.projects import ProjectContext
from repo_agent.tools.repository import IGNORED_DIRECTORIES


class CandidateWorkspaceError(RuntimeError):
    """候选工作副本创建、读取或清理失败。"""


class CandidateWorkspaceLimitError(CandidateWorkspaceError):
    """目标仓库超过工作副本的文件或字节预算。"""


class CandidateWorkspaceClosedError(CandidateWorkspaceError):
    """在工作副本打开前或关闭后访问内部状态。"""


@dataclass(frozen=True, slots=True)
class CandidateWorkspaceConfig:
    """工作副本复制与资源预算。"""

    max_files: int = 20_000
    max_total_bytes: int = 100 * 1024 * 1024
    max_single_file_bytes: int = 5 * 1024 * 1024
    keep_after_exit: bool = False

    def __post_init__(self) -> None:
        if self.max_files < 1:
            raise ValueError("max_files 必须大于等于 1")
        if self.max_total_bytes < 1:
            raise ValueError("max_total_bytes 必须大于等于 1")
        if self.max_single_file_bytes < 1:
            raise ValueError("max_single_file_bytes 必须大于等于 1")


def sha256_bytes(content: bytes) -> str:
    """返回文件内容的稳定 SHA-256。"""

    return hashlib.sha256(content).hexdigest()


class CandidateWorkspace:
    """复制目标仓库并保存基线内容，用于候选修改和差异比较。"""

    def __init__(
        self,
        source_context: ProjectContext,
        workspace_base: str | Path,
        run_id: str,
        *,
        config: CandidateWorkspaceConfig | None = None,
    ) -> None:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}", run_id):
            raise CandidateWorkspaceError(
                "run_id 必须以字母或数字开头，只能包含字母、数字、点、下划线和短横线"
            )
        self.source_context = source_context
        self.workspace_base = Path(workspace_base).expanduser().resolve()
        self.run_id = run_id
        self.config = config or CandidateWorkspaceConfig()
        self.run_root = self.workspace_base / run_id
        self.worktree_root = self.run_root / "worktree"
        self._baseline: dict[str, bytes] | None = None
        self._context: ProjectContext | None = None

    def __enter__(self) -> "CandidateWorkspace":
        """复制普通文件，跳过缓存、版本控制目录和符号链接。"""

        if self._baseline is not None:
            raise CandidateWorkspaceError("CandidateWorkspace 不能重复打开")
        try:
            self.workspace_base.relative_to(self.source_context.repo_root)
        except ValueError:
            pass
        else:
            raise CandidateWorkspaceError("工作副本目录不能位于目标仓库内部")
        if self.run_root.exists():
            raise CandidateWorkspaceError(f"候选工作目录已经存在：{self.run_root}")
        self.worktree_root.mkdir(parents=True)

        baseline: dict[str, bytes] = {}
        total_bytes = 0
        file_count = 0
        try:
            for current_root, directory_names, file_names in os.walk(
                self.source_context.repo_root,
                followlinks=False,
            ):
                current_path = Path(current_root)
                directory_names[:] = sorted(
                    name
                    for name in directory_names
                    if name not in IGNORED_DIRECTORIES
                    and not (current_path / name).is_symlink()
                )
                for file_name in sorted(file_names):
                    source = current_path / file_name
                    if source.is_symlink() or not source.is_file():
                        continue
                    size = source.stat().st_size
                    if size > self.config.max_single_file_bytes:
                        raise CandidateWorkspaceLimitError(
                            f"单个文件超过复制上限：{source}"
                        )
                    file_count += 1
                    total_bytes += size
                    if file_count > self.config.max_files:
                        raise CandidateWorkspaceLimitError("目标仓库文件数量超过工作副本上限")
                    if total_bytes > self.config.max_total_bytes:
                        raise CandidateWorkspaceLimitError("目标仓库总大小超过工作副本上限")
                    relative = source.relative_to(self.source_context.repo_root)
                    content = source.read_bytes()
                    destination = self.worktree_root / relative
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_bytes(content)
                    baseline[relative.as_posix()] = content
        except Exception:
            shutil.rmtree(self.run_root, ignore_errors=True)
            raise

        self._baseline = baseline
        self._context = replace(
            self.source_context,
            repo_root=self.worktree_root.resolve(),
            revision=f"candidate:{self.source_context.revision}:{self.run_id}",
            revision_kind="candidate",
            git_root=None,
            commit_sha=None,
            is_dirty=True,
            current_branch=None,
        )
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """默认清理候选副本，真实目标仓库始终不参与清理。"""

        self._baseline = None
        self._context = None
        if not self.config.keep_after_exit:
            shutil.rmtree(self.run_root, ignore_errors=True)

    @property
    def context(self) -> ProjectContext:
        """返回路径边界已经切换到工作副本的 ProjectContext。"""

        if self._context is None:
            raise CandidateWorkspaceClosedError("CandidateWorkspace 尚未打开或已经关闭")
        return self._context

    @property
    def baseline(self) -> dict[str, bytes]:
        """返回只供比较使用的基线内容副本。"""

        if self._baseline is None:
            raise CandidateWorkspaceClosedError("CandidateWorkspace 尚未打开或已经关闭")
        return dict(self._baseline)

    def current_files(self) -> dict[str, bytes]:
        """读取工作副本当前普通文件，忽略测试产生的缓存。"""

        _ = self.context
        current: dict[str, bytes] = {}
        total_bytes = 0
        file_count = 0
        for current_root, directory_names, file_names in os.walk(
            self.worktree_root,
            followlinks=False,
        ):
            current_path = Path(current_root)
            directory_names[:] = sorted(
                name
                for name in directory_names
                if name not in IGNORED_DIRECTORIES
                and not (current_path / name).is_symlink()
            )
            for file_name in sorted(file_names):
                path = current_path / file_name
                if path.is_symlink() or not path.is_file():
                    continue
                size = path.stat().st_size
                file_count += 1
                total_bytes += size
                if size > self.config.max_single_file_bytes:
                    raise CandidateWorkspaceLimitError(
                        f"工作副本产生了超大文件：{path}"
                    )
                if file_count > self.config.max_files:
                    raise CandidateWorkspaceLimitError("工作副本文件数量超过上限")
                if total_bytes > self.config.max_total_bytes:
                    raise CandidateWorkspaceLimitError("工作副本总大小超过上限")
                relative = path.relative_to(self.worktree_root).as_posix()
                current[relative] = path.read_bytes()
        return current

    def changed_files(self) -> tuple[str, ...]:
        """返回新增、删除或内容变化的相对路径。"""

        baseline = self.baseline
        current = self.current_files()
        all_paths = set(baseline) | set(current)
        return tuple(
            sorted(
                path
                for path in all_paths
                if baseline.get(path) != current.get(path)
            )
        )
