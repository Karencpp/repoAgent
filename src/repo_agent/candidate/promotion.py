"""把已验证候选修改安全回写到真实目标仓库。"""

from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
import tempfile

from repo_agent.projects import ProjectContext, inspect_repository

from .models import (
    CandidateEvaluationReport,
    CandidatePatch,
    CandidatePromotionResult,
)
from .workspace import sha256_bytes


class CandidatePromotionError(RuntimeError):
    """候选回写未满足批准、验证或版本约束。"""


class CandidatePromotionConflictError(CandidatePromotionError):
    """真实仓库在候选形成后发生了变化。"""


def _atomic_write(path: Path, content: bytes) -> None:
    """在同一目录创建临时文件后原子替换目标。"""

    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".repo-agent-tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink(missing_ok=True)


class CandidatePatchPromoter:
    """只接受通过客观验证且得到显式批准的候选补丁。"""

    def promote(
        self,
        context: ProjectContext,
        proposal_id: str,
        patch: CandidatePatch,
        evaluation: CandidateEvaluationReport,
        *,
        approved: bool,
    ) -> CandidatePromotionResult:
        """再次校验版本和文件哈希后执行可回滚回写。"""

        if not approved:
            raise CandidatePromotionError("候选回写需要显式批准")
        if not evaluation.passed:
            raise CandidatePromotionError("候选修改未通过全部客观验证，拒绝回写")
        patch_paths = tuple(change.path for change in patch.changes)
        if set(patch_paths) != set(evaluation.changed_files):
            raise CandidatePromotionError("评估报告与候选补丁的文件范围不一致")

        current_revision = inspect_repository(context.repo_root).revision
        if current_revision != context.revision:
            raise CandidatePromotionConflictError(
                "目标仓库版本在候选验证后已经变化，请重新生成候选"
            )

        staged: list[tuple[Path, bytes | None, bytes | None, str]] = []
        for change in patch.changes:
            path = context.resolve_repo_path(
                change.path,
                must_exist=change.operation != "create",
            )
            if change.operation == "create":
                if path.exists() or not path.parent.is_dir() or path.parent.is_symlink():
                    raise CandidatePromotionConflictError(
                        f"真实仓库无法安全创建文件：{change.path}"
                    )
                staged.append(
                    (
                        path,
                        None,
                        (change.replacement_content or "").encode("utf-8"),
                        change.operation,
                    )
                )
                continue
            if not path.is_file() or path.is_symlink():
                raise CandidatePromotionConflictError(
                    f"真实仓库目标不再是普通文件：{change.path}"
                )
            before = path.read_bytes()
            actual = sha256_bytes(before)
            if actual != change.expected_sha256:
                raise CandidatePromotionConflictError(
                    f"真实仓库文件哈希已经变化：{change.path}"
                )
            after = (
                None
                if change.operation == "delete"
                else (change.replacement_content or "").encode("utf-8")
            )
            staged.append((path, before, after, change.operation))

        written: list[tuple[Path, bytes | None]] = []
        try:
            for path, before, after, operation in staged:
                if operation == "delete":
                    path.unlink()
                else:
                    _atomic_write(path, after or b"")
                written.append((path, before))
        except OSError as exc:
            rollback_errors: list[str] = []
            for path, before in reversed(written):
                try:
                    if before is None:
                        path.unlink(missing_ok=True)
                    else:
                        _atomic_write(path, before)
                except OSError as rollback_exc:
                    rollback_errors.append(f"{path}: {rollback_exc}")
            detail = (
                "；回滚失败：" + " | ".join(rollback_errors)
                if rollback_errors
                else "；已回滚已写文件"
            )
            raise CandidatePromotionError(f"候选回写失败：{exc}{detail}") from exc

        return CandidatePromotionResult(
            proposal_id=proposal_id,
            project_id=context.project_id,
            source_revision=context.revision,
            changed_files=patch_paths,
            promoted_at=datetime.now(timezone.utc).isoformat(),
        )
