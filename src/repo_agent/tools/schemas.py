"""本地仓库工具的结构化输入输出。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True, slots=True)
class ListFilesInput:
    """文件树工具输入。"""

    max_depth: int = 4
    max_results: int = 300
    file_glob: str | None = None


@dataclass(frozen=True, slots=True)
class FileEntry:
    """代码库中的一个目录项。"""

    path: str
    kind: Literal["file", "directory"]
    size: int | None


@dataclass(frozen=True, slots=True)
class SearchCodeInput:
    """精确文本搜索工具输入。"""

    query: str
    file_glob: str | None = "*.py"
    case_sensitive: bool = False
    max_results: int = 50


@dataclass(frozen=True, slots=True)
class SearchMatch:
    """一条带路径、行号和列号的代码命中。"""

    path: str
    line_number: int
    column_number: int
    line: str


@dataclass(frozen=True, slots=True)
class ReadFileRangeInput:
    """局部文件读取工具输入。"""

    path: str
    start_line: int = 1
    end_line: int = 200
    max_chars: int = 20_000


@dataclass(frozen=True, slots=True)
class FileSlice:
    """局部文件内容及截断元数据。"""

    path: str
    start_line: int
    end_line: int
    total_lines: int
    content: str
    truncated: bool


@dataclass(frozen=True, slots=True)
class InspectPythonInput:
    """Python AST 分析工具输入。"""

    path: str
    symbol: str | None = None
    max_definitions: int = 200


@dataclass(frozen=True, slots=True)
class PythonImport:
    """Python 导入关系。"""

    module: str
    names: tuple[str, ...]
    line_number: int


@dataclass(frozen=True, slots=True)
class PythonDefinition:
    """Python 类、函数或异步函数定义摘要。"""

    kind: Literal["class", "function", "async_function"]
    name: str
    qualified_name: str
    line_number: int
    end_line: int
    signature: str
    decorators: tuple[str, ...]
    doc_summary: str | None


@dataclass(frozen=True, slots=True)
class PythonModuleInspection:
    """一个 Python 模块的结构化 AST 摘要。"""

    path: str
    imports: tuple[PythonImport, ...]
    definitions: tuple[PythonDefinition, ...]
    definition_limit_reached: bool


@dataclass(frozen=True, slots=True)
class RunPytestInput:
    """受限 pytest 工具输入。"""

    targets: tuple[str, ...] = ()
    keyword: str | None = None
    max_failures: int = 1
    timeout_seconds: float = 60.0
    output_limit: int = 20_000

