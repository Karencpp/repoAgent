"""不经过 Shell 的受限子进程执行器。"""

from __future__ import annotations

from pathlib import Path
import subprocess
import time
from typing import Protocol, Sequence

from .models import ProcessResult


class ProcessRunner(Protocol):
    """便于真实执行器与测试替身共享的进程接口。"""

    def run(
        self,
        command: Sequence[str],
        *,
        cwd: Path,
        timeout_seconds: float,
        output_limit: int,
    ) -> ProcessResult:
        """执行参数数组并返回结构化结果。"""


def _coerce_timeout_output(value: str | bytes | None) -> str:
    """统一不同 Python/平台下 TimeoutExpired 的输出类型。"""

    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _truncate_output(text: str, limit: int) -> tuple[str, bool]:
    """同时保留输出头尾，避免丢失命令上下文或最终错误。"""

    if len(text) <= limit:
        return text, False
    marker = "\n...<输出已截断>...\n"
    remaining = max(0, limit - len(marker))
    head_size = remaining // 2
    tail_size = remaining - head_size
    return f"{text[:head_size]}{marker}{text[-tail_size:]}", True


class SecureSubprocessRunner:
    """使用参数数组、固定工作目录、超时和输出上限执行子进程。"""

    def run(
        self,
        command: Sequence[str],
        *,
        cwd: Path,
        timeout_seconds: float,
        output_limit: int,
    ) -> ProcessResult:
        """执行命令，明确禁用 Shell 字符串解释。"""

        started_at = time.perf_counter()
        normalized_command = tuple(str(part) for part in command)
        try:
            completed = subprocess.run(
                normalized_command,
                cwd=cwd,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_seconds,
                shell=False,
            )
            stdout, stdout_truncated = _truncate_output(
                completed.stdout, output_limit
            )
            stderr, stderr_truncated = _truncate_output(
                completed.stderr, output_limit
            )
            return ProcessResult(
                command=normalized_command,
                exit_code=completed.returncode,
                stdout=stdout,
                stderr=stderr,
                duration_ms=int((time.perf_counter() - started_at) * 1000),
                timed_out=False,
                output_truncated=stdout_truncated or stderr_truncated,
            )
        except subprocess.TimeoutExpired as exc:
            stdout, stdout_truncated = _truncate_output(
                _coerce_timeout_output(exc.stdout), output_limit
            )
            stderr, stderr_truncated = _truncate_output(
                _coerce_timeout_output(exc.stderr), output_limit
            )
            return ProcessResult(
                command=normalized_command,
                exit_code=None,
                stdout=stdout,
                stderr=stderr,
                duration_ms=int((time.perf_counter() - started_at) * 1000),
                timed_out=True,
                output_truncated=stdout_truncated or stderr_truncated,
            )
        except OSError as exc:
            return ProcessResult(
                command=normalized_command,
                exit_code=None,
                stdout="",
                stderr=str(exc),
                duration_ms=int((time.perf_counter() - started_at) * 1000),
                timed_out=False,
                output_truncated=False,
                launch_error=True,
            )

