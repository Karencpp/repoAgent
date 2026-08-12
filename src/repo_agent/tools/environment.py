"""解析目标代码库用于测试的 Python 解释器。"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import sys
from typing import Literal

from repo_agent.projects import ProjectContext


@dataclass(frozen=True, slots=True)
class PythonRuntime:
    """Python 解释器路径及其选择来源。"""

    executable: Path
    source: Literal["repository_venv", "host"]


def resolve_python_runtime(context: ProjectContext) -> PythonRuntime:
    """优先选择仓库常见虚拟环境，否则明确降级到宿主解释器。"""

    windows_candidates = tuple(
        context.repo_root / name / "Scripts" / "python.exe"
        for name in (".venv", "venv", "env")
    )
    posix_candidates = tuple(
        context.repo_root / name / "bin" / "python"
        for name in (".venv", "venv", "env")
    )
    candidates = (
        (*windows_candidates, *posix_candidates)
        if os.name == "nt"
        else (*posix_candidates, *windows_candidates)
    )
    for candidate in candidates:
        if candidate.is_file():
            return PythonRuntime(
                executable=candidate.resolve(),
                source="repository_venv",
            )
    return PythonRuntime(
        executable=Path(sys.executable).resolve(),
        source="host",
    )
