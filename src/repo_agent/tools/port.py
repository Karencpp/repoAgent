"""仓库工具端口，隔离 Graph 与本地/MCP 具体实现。"""

from __future__ import annotations

from typing import Protocol

from .models import ProcessResult, ToolResult
from .schemas import (
    FileEntry,
    FileSlice,
    InspectPythonInput,
    ListFilesInput,
    PythonModuleInspection,
    ReadFileRangeInput,
    RunPytestInput,
    SearchCodeInput,
    SearchMatch,
)


class RepositoryToolPort(Protocol):
    """Graph 节点依赖的最小仓库工具契约。"""

    def list_files(self, request: ListFilesInput) -> ToolResult[tuple[FileEntry, ...]]:
        """列出受控深度内的目录项。"""

    def search_code(
        self, request: SearchCodeInput
    ) -> ToolResult[tuple[SearchMatch, ...]]:
        """在目标代码库内执行精确文本搜索。"""

    def read_file_range(self, request: ReadFileRangeInput) -> ToolResult[FileSlice]:
        """读取文本文件的有限行区间。"""

    def inspect_python(
        self, request: InspectPythonInput
    ) -> ToolResult[PythonModuleInspection]:
        """通过 AST 查看 Python 模块结构。"""

    def run_pytest(self, request: RunPytestInput) -> ToolResult[ProcessResult]:
        """在目标代码库沙箱内运行受限 pytest。"""

