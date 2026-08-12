"""仓库工具共享的结果与进程执行模型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Generic, Literal, TypeVar


T = TypeVar("T")


class ToolErrorKind(str, Enum):
    """供 Agent 路由和重试策略使用的稳定错误分类。"""

    INVALID_ARGUMENT = "invalid_argument"
    NOT_FOUND = "not_found"
    PERMISSION_DENIED = "permission_denied"
    PARSE_ERROR = "parse_error"
    TIMEOUT = "timeout"
    EXECUTION_ERROR = "execution_error"
    INPUT_REQUIRED = "input_required"
    INTERNAL_ERROR = "internal_error"


@dataclass(frozen=True, slots=True)
class ToolError:
    """结构化工具错误，不依赖模型解析自由文本。"""

    kind: ToolErrorKind
    message: str
    retryable: bool = False
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ToolResult(Generic[T]):
    """统一工具返回契约。

    ``status`` 表示工具基础设施是否正常完成调用，不表示业务目标一定成功。
    例如 pytest 返回非零退出码时，工具仍然成功提供了一条有效观察。
    """

    status: Literal["success", "error"]
    data: T | None = None
    error: ToolError | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        """工具调用是否正常完成。"""

        return self.status == "success"

    @classmethod
    def success(
        cls,
        data: T,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> "ToolResult[T]":
        """构造成功结果。"""

        return cls(status="success", data=data, metadata=metadata or {})

    @classmethod
    def failure(
        cls,
        kind: ToolErrorKind,
        message: str,
        *,
        retryable: bool = False,
        details: dict[str, Any] | None = None,
        data: T | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> "ToolResult[T]":
        """构造失败结果，同时允许携带超时前的部分观察。"""

        return cls(
            status="error",
            data=data,
            error=ToolError(
                kind=kind,
                message=message,
                retryable=retryable,
                details=details or {},
            ),
            metadata=metadata or {},
        )


@dataclass(frozen=True, slots=True)
class ProcessResult:
    """受限子进程的完整观察结果。"""

    command: tuple[str, ...]
    exit_code: int | None
    stdout: str
    stderr: str
    duration_ms: int
    timed_out: bool
    output_truncated: bool
    launch_error: bool = False
