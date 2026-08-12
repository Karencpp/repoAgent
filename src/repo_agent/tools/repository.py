"""绑定 ProjectContext 的本地受限仓库工具实现。"""

from __future__ import annotations

import ast
from fnmatch import fnmatch
import os
from pathlib import Path
from typing import Iterable, Literal

from repo_agent.projects import (
    InvalidRepositoryError,
    PathOutsideRepositoryError,
    ProjectContext,
)

from .models import ProcessResult, ToolErrorKind, ToolResult
from .environment import PythonRuntime, resolve_python_runtime
from .process import ProcessRunner, SecureSubprocessRunner
from .schemas import (
    FileEntry,
    FileSlice,
    InspectPythonInput,
    ListFilesInput,
    PythonDefinition,
    PythonImport,
    PythonModuleInspection,
    ReadFileRangeInput,
    RunPytestInput,
    SearchCodeInput,
    SearchMatch,
)


IGNORED_DIRECTORIES = {
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
MAX_SEARCH_FILE_BYTES = 2 * 1024 * 1024
MAX_AST_FILE_BYTES = 2 * 1024 * 1024
MAX_READ_LINES = 500


class ToolInputError(ValueError):
    """模型给出的工具参数不满足本地约束。"""


def _validate_glob(pattern: str | None) -> None:
    """拒绝可能表达代码库外路径的 glob。"""

    if pattern is None:
        return
    if not pattern or len(pattern) > 200:
        raise ToolInputError("file_glob 长度必须为 1 到 200")
    path_pattern = Path(pattern)
    if path_pattern.is_absolute() or ".." in path_pattern.parts:
        raise ToolInputError("file_glob 不能包含绝对路径或上级目录")


def _matches_glob(relative_path: Path, pattern: str | None) -> bool:
    """让简单文件名 glob 与相对路径 glob 都可使用。"""

    if pattern is None:
        return True
    posix_path = relative_path.as_posix()
    return fnmatch(posix_path, pattern) or fnmatch(relative_path.name, pattern)


def _first_doc_line(node: ast.AST) -> str | None:
    """读取定义文档字符串的第一条非空行。"""

    docstring = ast.get_docstring(node, clean=True)
    if not docstring:
        return None
    for line in docstring.splitlines():
        if line.strip():
            return line.strip()[:300]
    return None


def _safe_unparse(node: ast.AST) -> str:
    """将 AST 片段转换成文本，失败时返回占位符。"""

    try:
        return ast.unparse(node)
    except (TypeError, ValueError):
        return "<无法还原>"


class _DefinitionCollector(ast.NodeVisitor):
    """收集类、函数和异步函数，并保留限定名称。"""

    def __init__(self, max_definitions: int) -> None:
        self.max_definitions = max_definitions
        self.scope: list[str] = []
        self.definitions: list[PythonDefinition] = []
        self.limit_reached = False

    def _append(
        self,
        node: ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef,
        kind: Literal["class", "function", "async_function"],
        signature: str,
    ) -> None:
        if len(self.definitions) >= self.max_definitions:
            self.limit_reached = True
            return
        qualified_name = ".".join((*self.scope, node.name))
        self.definitions.append(
            PythonDefinition(
                kind=kind,
                name=node.name,
                qualified_name=qualified_name,
                line_number=node.lineno,
                end_line=node.end_lineno or node.lineno,
                signature=signature,
                decorators=tuple(_safe_unparse(item) for item in node.decorator_list),
                doc_summary=_first_doc_line(node),
            )
        )

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        bases = ", ".join(_safe_unparse(base) for base in node.bases)
        self._append(node, "class", f"class {node.name}({bases})" if bases else f"class {node.name}")
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._append(node, "function", f"def {node.name}({_safe_unparse(node.args)})")
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._append(
            node,
            "async_function",
            f"async def {node.name}({_safe_unparse(node.args)})",
        )
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()


class LocalRepositoryTools:
    """只在一个不可变 ProjectContext 内工作的本地工具集合。"""

    def __init__(
        self,
        context: ProjectContext,
        *,
        process_runner: ProcessRunner | None = None,
        allow_code_execution: bool = False,
        python_runtime: PythonRuntime | None = None,
    ) -> None:
        self.context = context
        self.process_runner = process_runner or SecureSubprocessRunner()
        self.allow_code_execution = allow_code_execution
        self.python_runtime = python_runtime or resolve_python_runtime(context)

    def _iter_files(self) -> Iterable[tuple[Path, Path]]:
        """按相对路径稳定排序遍历文件，并跳过缓存和外部链接。"""

        collected: list[tuple[Path, Path]] = []
        for current_root, directory_names, file_names in os.walk(
            self.context.repo_root
        ):
            current_path = Path(current_root)
            directory_names[:] = sorted(
                name
                for name in directory_names
                if name not in IGNORED_DIRECTORIES
                and not (current_path / name).is_symlink()
            )
            for file_name in sorted(file_names):
                candidate = current_path / file_name
                try:
                    resolved = self.context.resolve_repo_path(candidate)
                    relative = resolved.relative_to(self.context.repo_root)
                except (InvalidRepositoryError, PathOutsideRepositoryError, OSError):
                    continue
                if resolved.is_file():
                    collected.append((relative, resolved))
        yield from sorted(collected, key=lambda item: item[0].as_posix())

    def list_files(
        self, request: ListFilesInput
    ) -> ToolResult[tuple[FileEntry, ...]]:
        """列出有限深度和数量的目录项。"""

        try:
            if not 0 <= request.max_depth <= 20:
                raise ToolInputError("max_depth 必须在 0 到 20 之间")
            if not 1 <= request.max_results <= 5_000:
                raise ToolInputError("max_results 必须在 1 到 5000 之间")
            _validate_glob(request.file_glob)

            entries: list[FileEntry] = []
            limit_reached = False
            for current_root, directory_names, file_names in os.walk(
                self.context.repo_root
            ):
                current_path = Path(current_root)
                current_relative = current_path.relative_to(self.context.repo_root)
                current_depth = len(current_relative.parts)
                directory_names[:] = sorted(
                    name
                    for name in directory_names
                    if name not in IGNORED_DIRECTORIES
                    and not (current_path / name).is_symlink()
                )

                if current_depth < request.max_depth:
                    for directory_name in directory_names:
                        relative = current_relative / directory_name
                        if request.file_glob is not None and not _matches_glob(
                            relative, request.file_glob
                        ):
                            continue
                        entries.append(
                            FileEntry(
                                path=relative.as_posix(),
                                kind="directory",
                                size=None,
                            )
                        )
                        if len(entries) >= request.max_results:
                            limit_reached = True
                            break
                if limit_reached:
                    break

                for file_name in sorted(file_names):
                    candidate = current_path / file_name
                    try:
                        resolved = self.context.resolve_repo_path(candidate)
                        relative = resolved.relative_to(self.context.repo_root)
                    except (
                        InvalidRepositoryError,
                        PathOutsideRepositoryError,
                        OSError,
                    ):
                        continue
                    if len(relative.parts) - 1 > request.max_depth:
                        continue
                    if not _matches_glob(relative, request.file_glob):
                        continue
                    entries.append(
                        FileEntry(
                            path=relative.as_posix(),
                            kind="file",
                            size=resolved.stat().st_size,
                        )
                    )
                    if len(entries) >= request.max_results:
                        limit_reached = True
                        break
                if limit_reached:
                    break
                if current_depth >= request.max_depth:
                    directory_names.clear()

            return ToolResult.success(
                tuple(entries),
                metadata={"result_limit_reached": limit_reached},
            )
        except ToolInputError as exc:
            return ToolResult.failure(ToolErrorKind.INVALID_ARGUMENT, str(exc))
        except OSError as exc:
            return ToolResult.failure(
                ToolErrorKind.INTERNAL_ERROR,
                f"遍历代码库失败：{exc}",
                retryable=True,
            )

    def search_code(
        self, request: SearchCodeInput
    ) -> ToolResult[tuple[SearchMatch, ...]]:
        """在有限大小的文本文件中执行精确字符串搜索。"""

        try:
            if not request.query or len(request.query) > 500:
                raise ToolInputError("query 长度必须为 1 到 500")
            if not 1 <= request.max_results <= 1_000:
                raise ToolInputError("max_results 必须在 1 到 1000 之间")
            _validate_glob(request.file_glob)

            needle = request.query if request.case_sensitive else request.query.casefold()
            matches: list[SearchMatch] = []
            scanned_files = 0
            skipped_large_files = 0
            limit_reached = False

            for relative, file_path in self._iter_files():
                if not _matches_glob(relative, request.file_glob):
                    continue
                if file_path.stat().st_size > MAX_SEARCH_FILE_BYTES:
                    skipped_large_files += 1
                    continue
                scanned_files += 1
                try:
                    with file_path.open(
                        "r", encoding="utf-8-sig", errors="replace"
                    ) as handle:
                        for line_number, raw_line in enumerate(handle, start=1):
                            haystack = raw_line if request.case_sensitive else raw_line.casefold()
                            search_from = 0
                            while True:
                                column = haystack.find(needle, search_from)
                                if column < 0:
                                    break
                                matches.append(
                                    SearchMatch(
                                        path=relative.as_posix(),
                                        line_number=line_number,
                                        column_number=column + 1,
                                        line=raw_line.rstrip("\r\n")[:500],
                                    )
                                )
                                if len(matches) >= request.max_results:
                                    limit_reached = True
                                    break
                                search_from = column + max(1, len(needle))
                            if limit_reached:
                                break
                except OSError:
                    continue
                if limit_reached:
                    break

            return ToolResult.success(
                tuple(matches),
                metadata={
                    "scanned_files": scanned_files,
                    "skipped_large_files": skipped_large_files,
                    "result_limit_reached": limit_reached,
                },
            )
        except ToolInputError as exc:
            return ToolResult.failure(ToolErrorKind.INVALID_ARGUMENT, str(exc))
        except OSError as exc:
            return ToolResult.failure(
                ToolErrorKind.INTERNAL_ERROR,
                f"搜索代码失败：{exc}",
                retryable=True,
            )

    def read_file_range(self, request: ReadFileRangeInput) -> ToolResult[FileSlice]:
        """读取有限文本范围，避免将整文件无条件放入上下文。"""

        try:
            if request.start_line < 1:
                raise ToolInputError("start_line 必须大于等于 1")
            if request.end_line < request.start_line:
                raise ToolInputError("end_line 不能小于 start_line")
            if request.end_line - request.start_line + 1 > MAX_READ_LINES:
                raise ToolInputError(f"单次最多读取 {MAX_READ_LINES} 行")
            if not 1 <= request.max_chars <= 100_000:
                raise ToolInputError("max_chars 必须在 1 到 100000 之间")

            file_path = self.context.resolve_repo_path(request.path)
            if not file_path.is_file():
                return ToolResult.failure(
                    ToolErrorKind.NOT_FOUND,
                    f"目标不是文件：{request.path}",
                )

            selected_lines: list[str] = []
            total_lines = 0
            truncated = False
            current_chars = 0
            with file_path.open("r", encoding="utf-8-sig", errors="replace") as handle:
                for total_lines, raw_line in enumerate(handle, start=1):
                    if not request.start_line <= total_lines <= request.end_line:
                        continue
                    remaining = request.max_chars - current_chars
                    if remaining <= 0:
                        truncated = True
                        continue
                    if len(raw_line) > remaining:
                        selected_lines.append(raw_line[:remaining])
                        current_chars += remaining
                        truncated = True
                    else:
                        selected_lines.append(raw_line)
                        current_chars += len(raw_line)

            actual_end = min(request.end_line, total_lines)
            relative = file_path.relative_to(self.context.repo_root).as_posix()
            return ToolResult.success(
                FileSlice(
                    path=relative,
                    start_line=request.start_line,
                    end_line=actual_end,
                    total_lines=total_lines,
                    content="".join(selected_lines),
                    truncated=truncated,
                )
            )
        except ToolInputError as exc:
            return ToolResult.failure(ToolErrorKind.INVALID_ARGUMENT, str(exc))
        except PathOutsideRepositoryError as exc:
            return ToolResult.failure(ToolErrorKind.PERMISSION_DENIED, str(exc))
        except InvalidRepositoryError as exc:
            return ToolResult.failure(ToolErrorKind.NOT_FOUND, str(exc))
        except OSError as exc:
            return ToolResult.failure(
                ToolErrorKind.INTERNAL_ERROR,
                f"读取文件失败：{exc}",
                retryable=True,
            )

    def inspect_python(
        self, request: InspectPythonInput
    ) -> ToolResult[PythonModuleInspection]:
        """使用 AST 提取导入和定义，不执行目标代码。"""

        try:
            if not 1 <= request.max_definitions <= 1_000:
                raise ToolInputError("max_definitions 必须在 1 到 1000 之间")
            if request.symbol is not None and not request.symbol.strip():
                raise ToolInputError("symbol 不能为空字符串")

            file_path = self.context.resolve_repo_path(request.path)
            if file_path.suffix.casefold() != ".py" or not file_path.is_file():
                raise ToolInputError("AST 工具只接受存在的 .py 文件")
            if file_path.stat().st_size > MAX_AST_FILE_BYTES:
                raise ToolInputError(
                    f"Python 文件超过 {MAX_AST_FILE_BYTES} 字节的 AST 限制"
                )

            source = file_path.read_text(encoding="utf-8-sig", errors="replace")
            try:
                tree = ast.parse(source, filename=str(file_path))
            except SyntaxError as exc:
                return ToolResult.failure(
                    ToolErrorKind.PARSE_ERROR,
                    f"Python 语法解析失败：{exc.msg}",
                    details={"line_number": exc.lineno, "offset": exc.offset},
                )

            imports: list[PythonImport] = []
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imports.append(
                        PythonImport(
                            module="",
                            names=tuple(alias.name for alias in node.names),
                            line_number=node.lineno,
                        )
                    )
                elif isinstance(node, ast.ImportFrom):
                    prefix = "." * node.level
                    imports.append(
                        PythonImport(
                            module=f"{prefix}{node.module or ''}",
                            names=tuple(alias.name for alias in node.names),
                            line_number=node.lineno,
                        )
                    )

            collector = _DefinitionCollector(request.max_definitions)
            collector.visit(tree)
            definitions = collector.definitions
            if request.symbol is not None:
                symbol = request.symbol.strip()
                definitions = [
                    item
                    for item in definitions
                    if item.name == symbol or item.qualified_name == symbol
                ]

            relative = file_path.relative_to(self.context.repo_root).as_posix()
            return ToolResult.success(
                PythonModuleInspection(
                    path=relative,
                    imports=tuple(sorted(imports, key=lambda item: item.line_number)),
                    definitions=tuple(definitions),
                    definition_limit_reached=collector.limit_reached,
                )
            )
        except ToolInputError as exc:
            return ToolResult.failure(ToolErrorKind.INVALID_ARGUMENT, str(exc))
        except PathOutsideRepositoryError as exc:
            return ToolResult.failure(ToolErrorKind.PERMISSION_DENIED, str(exc))
        except InvalidRepositoryError as exc:
            return ToolResult.failure(ToolErrorKind.NOT_FOUND, str(exc))
        except OSError as exc:
            return ToolResult.failure(
                ToolErrorKind.INTERNAL_ERROR,
                f"分析 Python 文件失败：{exc}",
                retryable=True,
            )

    def run_pytest(self, request: RunPytestInput) -> ToolResult[ProcessResult]:
        """构造固定 pytest 命令并在目标代码库内执行。"""

        try:
            if not self.allow_code_execution:
                return ToolResult.failure(
                    ToolErrorKind.PERMISSION_DENIED,
                    "pytest 会执行目标项目代码，当前运行未获得代码执行授权",
                )
            if len(request.targets) > 20:
                raise ToolInputError("单次最多指定 20 个 pytest target")
            if not 1 <= request.max_failures <= 20:
                raise ToolInputError("max_failures 必须在 1 到 20 之间")
            if not 0.1 <= request.timeout_seconds <= 300:
                raise ToolInputError("timeout_seconds 必须在 0.1 到 300 之间")
            if not 1_000 <= request.output_limit <= 100_000:
                raise ToolInputError("output_limit 必须在 1000 到 100000 之间")
            if request.keyword is not None:
                if not request.keyword.strip() or len(request.keyword) > 200:
                    raise ToolInputError("keyword 长度必须为 1 到 200")
                if "\x00" in request.keyword:
                    raise ToolInputError("keyword 不能包含空字节")

            normalized_targets: list[str] = []
            for target in request.targets:
                if not target or target.startswith("-"):
                    raise ToolInputError("pytest target 不能为空或以选项前缀开头")
                path_part, separator, node_id = target.partition("::")
                resolved = self.context.resolve_repo_path(path_part)
                relative = resolved.relative_to(self.context.repo_root).as_posix()
                normalized_targets.append(
                    f"{relative}::{node_id}" if separator else relative
                )

            command = [
                str(self.python_runtime.executable),
                "-m",
                "pytest",
                "-q",
                "--disable-warnings",
                f"--maxfail={request.max_failures}",
                *normalized_targets,
            ]
            if request.keyword is not None:
                command.extend(["-k", request.keyword])

            process_result = self.process_runner.run(
                command,
                cwd=self.context.repo_root,
                timeout_seconds=request.timeout_seconds,
                output_limit=request.output_limit,
            )
            if process_result.timed_out:
                return ToolResult.failure(
                    ToolErrorKind.TIMEOUT,
                    "pytest 执行超时",
                    retryable=True,
                    data=process_result,
                )
            if process_result.launch_error:
                return ToolResult.failure(
                    ToolErrorKind.EXECUTION_ERROR,
                    "pytest 进程启动失败",
                    data=process_result,
                )

            return ToolResult.success(
                process_result,
                metadata={
                    "tests_passed": process_result.exit_code == 0,
                    "test_exit_code": process_result.exit_code,
                    "python_executable": str(self.python_runtime.executable),
                    "python_runtime_source": self.python_runtime.source,
                },
            )
        except ToolInputError as exc:
            return ToolResult.failure(ToolErrorKind.INVALID_ARGUMENT, str(exc))
        except PathOutsideRepositoryError as exc:
            return ToolResult.failure(ToolErrorKind.PERMISSION_DENIED, str(exc))
        except InvalidRepositoryError as exc:
            return ToolResult.failure(ToolErrorKind.NOT_FOUND, str(exc))
