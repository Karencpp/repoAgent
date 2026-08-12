"""目标代码库选择、注册、版本识别与隔离。

本模块不会回退到进程当前工作目录。调用方必须显式提供代码库路径或已注册
项目。最终生成的 :class:`ProjectContext` 将作为后续图编排、记忆、检索和
工具模块的命名空间与沙箱边界。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
from typing import Any, Iterable
from uuid import uuid4


REGISTRY_SCHEMA_VERSION = 1
PROJECT_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
IGNORED_FINGERPRINT_DIRECTORIES = {
    ".git",
    ".idea",
    ".mypy_cache",
    ".pytest_cache",
    ".repo-agent",
    ".ruff_cache",
    ".tox",
    ".venv",
    "__pycache__",
    "node_modules",
    "venv",
}


class ProjectContextError(ValueError):
    """项目选择与隔离错误的基类。"""


class ProjectSelectionRequiredError(ProjectContextError):
    """未提供代码库路径或项目标识时抛出。"""


class AmbiguousProjectSelectionError(ProjectContextError):
    """同时提供代码库路径和项目标识时抛出。"""


class InvalidRepositoryError(ProjectContextError):
    """所选代码库路径无效时抛出。"""


class InvalidProjectNameError(ProjectContextError):
    """项目别名不适合作为安全标识时抛出。"""


class DuplicateProjectNameError(ProjectContextError):
    """项目别名已属于其他代码库时抛出。"""


class DuplicateProjectPathError(ProjectContextError):
    """代码库路径已使用其他别名注册时抛出。"""


class ProjectNotFoundError(ProjectContextError):
    """找不到已注册项目时抛出。"""


class PathOutsideRepositoryError(ProjectContextError):
    """工具路径逃逸所选代码库沙箱时抛出。"""


class RegistryCorruptedError(ProjectContextError):
    """持久化注册表不满足数据结构约束时抛出。"""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalized_path_key(path: Path) -> str:
    """返回适合路径身份比较且感知操作系统规则的键。"""

    return os.path.normcase(str(path.resolve()))


def _canonical_directory(path: str | os.PathLike[str]) -> Path:
    if isinstance(path, str) and not path.strip():
        raise InvalidRepositoryError(
            "Target repository path cannot be empty; the current working directory "
            "is never used implicitly"
        )
    candidate = Path(path).expanduser().resolve()
    if not candidate.exists():
        raise InvalidRepositoryError(f"Target repository does not exist: {candidate}")
    if not candidate.is_dir():
        raise InvalidRepositoryError(f"Target repository is not a directory: {candidate}")
    return candidate


def _run_git(repo_root: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["git", "-C", str(repo_root), *arguments],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(
            args=["git", "-C", str(repo_root), *arguments],
            returncode=127,
            stdout="",
            stderr=str(exc),
        )


def _git_value(repo_root: Path, *arguments: str) -> str | None:
    result = _run_git(repo_root, *arguments)
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    return value or None


def _manifest_fingerprint(repo_root: Path) -> str:
    """在不读取完整文件内容的前提下生成低成本版本指纹。

    单文件内容哈希属于后续检索和索引层的职责。这里的指纹只用于判断非 Git
    目录或存在未提交修改的 Git 工作区是否应被视为新版本，从而让缓存失效。
    """

    entries: list[str] = []
    for current_root, directory_names, file_names in os.walk(repo_root):
        directory_names[:] = sorted(
            name
            for name in directory_names
            if name not in IGNORED_FINGERPRINT_DIRECTORIES
        )
        current_path = Path(current_root)
        for file_name in sorted(file_names):
            file_path = current_path / file_name
            try:
                stat = file_path.stat()
                relative = file_path.relative_to(repo_root).as_posix()
            except (OSError, ValueError):
                continue
            entries.append(f"{relative}\0{stat.st_size}\0{stat.st_mtime_ns}")

    digest = hashlib.sha256("\n".join(entries).encode("utf-8")).hexdigest()
    return digest[:24]


@dataclass(frozen=True, slots=True)
class RepositoryInspection:
    """一次目标代码库检查得到的身份与版本信息。"""

    repo_root: Path
    revision: str
    revision_kind: str
    is_git: bool
    git_root: Path | None
    commit_sha: str | None
    is_dirty: bool
    git_remote: str | None
    current_branch: str | None


def inspect_repository(path: str | os.PathLike[str]) -> RepositoryInspection:
    """在不修改目标代码库的前提下检查其当前状态。

    所选路径可以是较大 Git 仓库中的子目录。用户选择的路径仍然作为沙箱边界，
    ``git_root`` 只记录用于读取提交元数据的上层 Git 仓库。
    """

    repo_root = _canonical_directory(path)
    git_top_level = _git_value(repo_root, "rev-parse", "--show-toplevel")

    if git_top_level is None:
        manifest = _manifest_fingerprint(repo_root)
        return RepositoryInspection(
            repo_root=repo_root,
            revision=f"manifest:{manifest}",
            revision_kind="manifest",
            is_git=False,
            git_root=None,
            commit_sha=None,
            is_dirty=False,
            git_remote=None,
            current_branch=None,
        )

    git_root = Path(git_top_level).resolve()
    commit_sha = _git_value(repo_root, "rev-parse", "HEAD")
    branch = _git_value(repo_root, "branch", "--show-current")
    remote = _git_value(repo_root, "config", "--get", "remote.origin.url")
    tracked_files = _git_value(repo_root, "ls-files", "--", ".")
    status_result = _run_git(repo_root, "status", "--porcelain", "--", ".")
    is_dirty = status_result.returncode == 0 and bool(status_result.stdout.strip())

    if commit_sha is None:
        manifest = _manifest_fingerprint(repo_root)
        revision = f"git-unborn:{manifest}"
        revision_kind = "git-unborn"
    elif repo_root != git_root and tracked_files is None:
        # 所选目录可能只是父仓库中的忽略或未跟踪目录，父提交无法代表其内容。
        manifest = _manifest_fingerprint(repo_root)
        revision = f"git-subtree:{commit_sha}:manifest:{manifest}"
        revision_kind = "git-subtree-manifest"
    elif is_dirty:
        manifest = _manifest_fingerprint(repo_root)
        revision = f"git:{commit_sha}:dirty:{manifest}"
        revision_kind = "git-dirty"
    else:
        revision = f"git:{commit_sha}:clean"
        revision_kind = "git-clean"

    return RepositoryInspection(
        repo_root=repo_root,
        revision=revision,
        revision_kind=revision_kind,
        is_git=True,
        git_root=git_root,
        commit_sha=commit_sha,
        is_dirty=is_dirty,
        git_remote=remote,
        current_branch=branch,
    )


@dataclass(frozen=True, slots=True)
class ProjectRegistration:
    """独立于当前代码版本持久化的稳定项目身份。"""

    project_id: str
    display_name: str
    canonical_repo_root: str
    git_remote: str | None
    registered_at: str
    updated_at: str
    last_seen_revision: str

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ProjectRegistration":
        if not isinstance(data, dict):
            raise RegistryCorruptedError("Project registry entry must be an object")
        required = {
            "project_id",
            "display_name",
            "canonical_repo_root",
            "registered_at",
            "updated_at",
            "last_seen_revision",
        }
        missing = required.difference(data)
        if missing:
            raise RegistryCorruptedError(
                f"Project registry entry is missing fields: {sorted(missing)}"
            )
        return cls(
            project_id=str(data["project_id"]),
            display_name=str(data["display_name"]),
            canonical_repo_root=str(data["canonical_repo_root"]),
            git_remote=(str(data["git_remote"]) if data.get("git_remote") else None),
            registered_at=str(data["registered_at"]),
            updated_at=str(data["updated_at"]),
            last_seen_revision=str(data["last_seen_revision"]),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ProjectRegistry:
    """使用 JSON 持久化并为目标代码库分配稳定标识的注册表。"""

    def __init__(self, storage_path: str | os.PathLike[str]) -> None:
        self.storage_path = Path(storage_path).expanduser().resolve()

    @classmethod
    def default(cls) -> "ProjectRegistry":
        app_home = os.environ.get("REPO_AGENT_HOME")
        root = Path(app_home).expanduser() if app_home else Path.home() / ".repo-agent"
        return cls(root / "projects.json")

    def list(self) -> tuple[ProjectRegistration, ...]:
        return tuple(sorted(self._load(), key=lambda item: item.display_name.casefold()))

    def register(
        self,
        repo_root: str | os.PathLike[str],
        display_name: str,
    ) -> ProjectRegistration:
        self._validate_name(display_name)
        inspection = inspect_repository(repo_root)
        projects = self._load()
        name_key = display_name.casefold()
        path_key = _normalized_path_key(inspection.repo_root)

        for existing in projects:
            existing_name_key = existing.display_name.casefold()
            existing_path_key = _normalized_path_key(Path(existing.canonical_repo_root))
            if existing_name_key == name_key and existing_path_key == path_key:
                refreshed = replace(
                    existing,
                    git_remote=inspection.git_remote,
                    updated_at=_utc_now(),
                    last_seen_revision=inspection.revision,
                )
                self._replace(projects, refreshed)
                self._save(projects)
                return refreshed
            if existing_name_key == name_key:
                raise DuplicateProjectNameError(
                    f"Project name '{display_name}' already refers to "
                    f"{existing.canonical_repo_root}"
                )
            if existing_path_key == path_key:
                raise DuplicateProjectPathError(
                    f"Repository '{inspection.repo_root}' is already registered as "
                    f"'{existing.display_name}'"
                )

        now = _utc_now()
        registration = ProjectRegistration(
            project_id=f"project-{uuid4().hex}",
            display_name=display_name,
            canonical_repo_root=str(inspection.repo_root),
            git_remote=inspection.git_remote,
            registered_at=now,
            updated_at=now,
            last_seen_revision=inspection.revision,
        )
        projects.append(registration)
        self._save(projects)
        return registration

    def get(self, selector: str) -> ProjectRegistration:
        selector_key = selector.casefold()
        for project in self._load():
            if project.project_id.casefold() == selector_key:
                return project
            if project.display_name.casefold() == selector_key:
                return project
        raise ProjectNotFoundError(f"Registered project not found: {selector}")

    def update_path(
        self,
        selector: str,
        new_repo_root: str | os.PathLike[str],
    ) -> ProjectRegistration:
        inspection = inspect_repository(new_repo_root)
        projects = self._load()
        current = self._find(projects, selector)
        new_path_key = _normalized_path_key(inspection.repo_root)

        for existing in projects:
            if existing.project_id == current.project_id:
                continue
            if _normalized_path_key(Path(existing.canonical_repo_root)) == new_path_key:
                raise DuplicateProjectPathError(
                    f"Repository '{inspection.repo_root}' is already registered as "
                    f"'{existing.display_name}'"
                )

        updated = replace(
            current,
            canonical_repo_root=str(inspection.repo_root),
            git_remote=inspection.git_remote,
            updated_at=_utc_now(),
            last_seen_revision=inspection.revision,
        )
        self._replace(projects, updated)
        self._save(projects)
        return updated

    def refresh(self, selector: str) -> ProjectRegistration:
        projects = self._load()
        current = self._find(projects, selector)
        inspection = inspect_repository(current.canonical_repo_root)
        refreshed = replace(
            current,
            git_remote=inspection.git_remote,
            updated_at=_utc_now(),
            last_seen_revision=inspection.revision,
        )
        self._replace(projects, refreshed)
        self._save(projects)
        return refreshed

    def _load(self) -> list[ProjectRegistration]:
        if not self.storage_path.exists():
            return []
        try:
            payload = json.loads(self.storage_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RegistryCorruptedError(
                f"Cannot read project registry '{self.storage_path}': {exc}"
            ) from exc

        if not isinstance(payload, dict):
            raise RegistryCorruptedError("Project registry root must be an object")
        if payload.get("schema_version") != REGISTRY_SCHEMA_VERSION:
            raise RegistryCorruptedError(
                f"Unsupported project registry schema: {payload.get('schema_version')}"
            )
        raw_projects = payload.get("projects")
        if not isinstance(raw_projects, list):
            raise RegistryCorruptedError("Project registry 'projects' must be a list")
        return [ProjectRegistration.from_dict(item) for item in raw_projects]

    def _save(self, projects: Iterable[ProjectRegistration]) -> None:
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": REGISTRY_SCHEMA_VERSION,
            "projects": [project.to_dict() for project in projects],
        }
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.storage_path.parent,
                prefix=f"{self.storage_path.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temporary_path = Path(handle.name)
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, self.storage_path)
        finally:
            if temporary_path is not None and temporary_path.exists():
                temporary_path.unlink(missing_ok=True)

    @staticmethod
    def _validate_name(display_name: str) -> None:
        if not PROJECT_NAME_PATTERN.fullmatch(display_name):
            raise InvalidProjectNameError(
                "Project name must be 1-64 characters and contain only letters, "
                "numbers, dot, underscore, or hyphen"
            )

    @staticmethod
    def _find(
        projects: Iterable[ProjectRegistration], selector: str
    ) -> ProjectRegistration:
        selector_key = selector.casefold()
        for project in projects:
            if project.project_id.casefold() == selector_key:
                return project
            if project.display_name.casefold() == selector_key:
                return project
        raise ProjectNotFoundError(f"Registered project not found: {selector}")

    @staticmethod
    def _replace(
        projects: list[ProjectRegistration], updated: ProjectRegistration
    ) -> None:
        for index, project in enumerate(projects):
            if project.project_id == updated.project_id:
                projects[index] = updated
                return
        raise ProjectNotFoundError(f"Registered project not found: {updated.project_id}")


@dataclass(frozen=True, slots=True)
class ProjectContext:
    """单次运行内不可变的目标代码库边界与命名空间。"""

    project_id: str
    display_name: str
    repo_root: Path
    revision: str
    revision_kind: str
    registered: bool
    git_root: Path | None
    commit_sha: str | None
    is_dirty: bool
    git_remote: str | None
    current_branch: str | None
    memory_namespace: str
    rag_namespace: str
    checkpoint_namespace: str

    def resolve_repo_path(
        self,
        candidate: str | os.PathLike[str],
        *,
        must_exist: bool = True,
    ) -> Path:
        """解析工具路径，并拒绝目录穿越和根目录外的绝对路径。"""

        supplied = Path(candidate).expanduser()
        combined = supplied if supplied.is_absolute() else self.repo_root / supplied
        resolved = combined.resolve(strict=False)
        try:
            resolved.relative_to(self.repo_root)
        except ValueError as exc:
            raise PathOutsideRepositoryError(
                f"Path escapes target repository '{self.repo_root}': {candidate}"
            ) from exc
        if must_exist and not resolved.exists():
            raise InvalidRepositoryError(f"Repository path does not exist: {resolved}")
        return resolved


class ProjectContextResolver:
    """根据唯一且显式的项目选择创建单次运行上下文。"""

    def __init__(self, registry: ProjectRegistry) -> None:
        self.registry = registry

    def resolve(
        self,
        *,
        repo: str | os.PathLike[str] | None = None,
        project: str | None = None,
    ) -> ProjectContext:
        if repo is None and project is None:
            raise ProjectSelectionRequiredError(
                "Select a target repository with 'repo' or a registered 'project'; "
                "the current working directory is never used implicitly"
            )
        if repo is not None and project is not None:
            raise AmbiguousProjectSelectionError(
                "Provide either 'repo' or 'project', not both"
            )

        if project is not None:
            registration = self.registry.get(project)
            inspection = inspect_repository(registration.canonical_repo_root)
            registration = self.registry.refresh(registration.project_id)
            project_id = registration.project_id
            display_name = registration.display_name
            registered = True
        else:
            if repo is None:
                raise ProjectSelectionRequiredError("必须显式指定目标代码库")
            inspection = inspect_repository(repo)
            path_key = _normalized_path_key(inspection.repo_root)
            digest = hashlib.sha256(path_key.encode("utf-8")).hexdigest()[:20]
            project_id = f"adhoc-{digest}"
            display_name = inspection.repo_root.name
            registered = False

        return ProjectContext(
            project_id=project_id,
            display_name=display_name,
            repo_root=inspection.repo_root,
            revision=inspection.revision,
            revision_kind=inspection.revision_kind,
            registered=registered,
            git_root=inspection.git_root,
            commit_sha=inspection.commit_sha,
            is_dirty=inspection.is_dirty,
            git_remote=inspection.git_remote,
            current_branch=inspection.current_branch,
            memory_namespace=f"projects/{project_id}/memory",
            rag_namespace=f"projects/{project_id}/rag",
            checkpoint_namespace=f"projects/{project_id}/checkpoints",
        )
