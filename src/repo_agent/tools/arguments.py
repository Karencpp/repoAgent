"""模型工具参数的 Pydantic 校验边界。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .schemas import (
    InspectPythonInput,
    ListFilesInput,
    ReadFileRangeInput,
    RunPytestInput,
    SearchCodeInput,
)


class ToolArguments(BaseModel):
    """所有模型工具参数的公共约束。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    def to_request(self) -> Any:
        """转换成内部工具请求对象。"""

        raise NotImplementedError


def _validate_model_glob(value: str | None) -> str | None:
    """在模型边界拒绝绝对路径和上级目录 glob。"""

    if value is None:
        return None
    if not value:
        raise ValueError("file_glob 不能为空")
    path_pattern = Path(value)
    if path_pattern.is_absolute() or ".." in path_pattern.parts:
        raise ValueError("file_glob 不能包含绝对路径或上级目录")
    return value


class ListFilesArguments(ToolArguments):
    """文件树工具的模型参数。"""

    max_depth: int = Field(default=4, ge=0, le=20)
    max_results: int = Field(default=300, ge=1, le=5_000)
    file_glob: str | None = Field(default=None, max_length=200)

    _safe_glob = field_validator("file_glob")(_validate_model_glob)

    def to_request(self) -> ListFilesInput:
        """转换成文件树内部请求。"""

        return ListFilesInput(**self.model_dump())


class SearchCodeArguments(ToolArguments):
    """精确代码搜索工具的模型参数。"""

    query: str = Field(min_length=1, max_length=500)
    file_glob: str | None = Field(default="*.py", max_length=200)
    case_sensitive: bool = False
    max_results: int = Field(default=50, ge=1, le=1_000)

    _safe_glob = field_validator("file_glob")(_validate_model_glob)

    @field_validator("query")
    @classmethod
    def validate_query(cls, value: str) -> str:
        """拒绝只有空白的查询。"""

        if not value.strip():
            raise ValueError("query 不能只包含空白")
        return value

    def to_request(self) -> SearchCodeInput:
        """转换成代码搜索内部请求。"""

        return SearchCodeInput(**self.model_dump())


class ReadFileRangeArguments(ToolArguments):
    """局部文件读取工具的模型参数。"""

    path: str = Field(min_length=1, max_length=1_000)
    start_line: int = Field(default=1, ge=1)
    end_line: int = Field(default=200, ge=1)
    max_chars: int = Field(default=20_000, ge=1, le=100_000)

    @model_validator(mode="after")
    def validate_line_range(self) -> "ReadFileRangeArguments":
        """限制行号顺序和单次读取规模。"""

        if self.end_line < self.start_line:
            raise ValueError("end_line 不能小于 start_line")
        if self.end_line - self.start_line + 1 > 500:
            raise ValueError("单次最多读取 500 行")
        return self

    def to_request(self) -> ReadFileRangeInput:
        """转换成局部读取内部请求。"""

        return ReadFileRangeInput(**self.model_dump())


class InspectPythonArguments(ToolArguments):
    """Python AST 工具的模型参数。"""

    path: str = Field(min_length=1, max_length=1_000)
    symbol: str | None = Field(default=None, max_length=500)
    max_definitions: int = Field(default=200, ge=1, le=1_000)

    @field_validator("symbol")
    @classmethod
    def validate_symbol(cls, value: str | None) -> str | None:
        """拒绝只有空白的符号过滤条件。"""

        if value is not None and not value.strip():
            raise ValueError("symbol 不能只包含空白")
        return value

    def to_request(self) -> InspectPythonInput:
        """转换成 AST 内部请求。"""

        return InspectPythonInput(**self.model_dump())


class RunPytestArguments(ToolArguments):
    """受限 pytest 工具的模型参数。"""

    targets: tuple[str, ...] = Field(default=(), max_length=20)
    keyword: str | None = Field(default=None, max_length=200)
    max_failures: int = Field(default=1, ge=1, le=20)
    timeout_seconds: float = Field(default=60.0, ge=0.1, le=300)
    output_limit: int = Field(default=20_000, ge=1_000, le=100_000)

    @field_validator("targets")
    @classmethod
    def validate_targets(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """拒绝空 target 和伪装成 pytest 选项的 target。"""

        for target in value:
            if not target or target.startswith("-"):
                raise ValueError("pytest target 不能为空或以选项前缀开头")
        return value

    @field_validator("keyword")
    @classmethod
    def validate_keyword(cls, value: str | None) -> str | None:
        """限制 pytest 关键字表达式。"""

        if value is not None:
            if not value.strip():
                raise ValueError("keyword 不能只包含空白")
            if "\x00" in value:
                raise ValueError("keyword 不能包含空字节")
        return value

    def to_request(self) -> RunPytestInput:
        """转换成 pytest 内部请求。"""

        return RunPytestInput(**self.model_dump())

