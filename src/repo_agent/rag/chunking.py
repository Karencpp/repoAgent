"""面向 Python、Markdown 和普通文本的结构感知分块。"""

from __future__ import annotations

import ast
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
from typing import Iterable

from repo_agent.projects import ProjectContext

from .models import RepositoryChunkDraft


DEFAULT_INDEX_EXTENSIONS = frozenset(
    {".py", ".md", ".txt", ".toml", ".yaml", ".yml", ".json"}
)
IGNORED_INDEX_DIRECTORIES = frozenset(
    {
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
)
_MARKDOWN_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*$")


@dataclass(frozen=True, slots=True)
class RepositoryChunkerConfig:
    """代码库遍历和分块预算。"""

    max_files: int = 10_000
    max_file_bytes: int = 512_000
    max_chunk_chars: int = 4_000
    overlap_lines: int = 3
    extensions: frozenset[str] = DEFAULT_INDEX_EXTENSIONS

    def __post_init__(self) -> None:
        if self.max_files < 1:
            raise ValueError("max_files 必须大于等于 1")
        if self.max_file_bytes < 1_000:
            raise ValueError("max_file_bytes 必须大于等于 1000")
        if not 500 <= self.max_chunk_chars <= 20_000:
            raise ValueError("max_chunk_chars 必须在 500 到 20000 之间")
        if not 0 <= self.overlap_lines <= 20:
            raise ValueError("overlap_lines 必须在 0 到 20 之间")


@dataclass(frozen=True, slots=True)
class RepositorySourceFile:
    """一次安全扫描发现的可索引文本文件。"""

    path: str
    absolute_path: Path
    content: str
    content_hash: str


@dataclass(frozen=True, slots=True)
class RepositoryScan:
    """文件扫描结果和被预算跳过的数量。"""

    files: tuple[RepositorySourceFile, ...]
    skipped_files: int


def _sha256_text(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


class RepositoryChunker:
    """只读取显式 ProjectContext 内的有限文本文件。"""

    def __init__(self, config: RepositoryChunkerConfig | None = None) -> None:
        self.config = config or RepositoryChunkerConfig()

    def scan(self, context: ProjectContext) -> RepositoryScan:
        """稳定遍历代码库，跳过链接、缓存、大文件和非 UTF-8 文本。"""

        files: list[RepositorySourceFile] = []
        skipped = 0
        for current_root, directory_names, file_names in os.walk(context.repo_root):
            current = Path(current_root)
            directory_names[:] = sorted(
                name
                for name in directory_names
                if name not in IGNORED_INDEX_DIRECTORIES
                and not (current / name).is_symlink()
            )
            for file_name in sorted(file_names):
                candidate = current / file_name
                if len(files) >= self.config.max_files:
                    skipped += 1
                    continue
                if candidate.is_symlink() or candidate.suffix.casefold() not in self.config.extensions:
                    skipped += 1
                    continue
                try:
                    resolved = context.resolve_repo_path(candidate)
                    if not resolved.is_file() or resolved.stat().st_size > self.config.max_file_bytes:
                        skipped += 1
                        continue
                    content = resolved.read_text(encoding="utf-8-sig")
                    relative = resolved.relative_to(context.repo_root).as_posix()
                except (OSError, UnicodeError, ValueError):
                    skipped += 1
                    continue
                files.append(
                    RepositorySourceFile(
                        path=relative,
                        absolute_path=resolved,
                        content=content,
                        content_hash=_sha256_text(content),
                    )
                )
        return RepositoryScan(
            files=tuple(sorted(files, key=lambda item: item.path)),
            skipped_files=skipped,
        )

    def chunk(self, source: RepositorySourceFile) -> tuple[RepositoryChunkDraft, ...]:
        """按文件类型选择结构化分块策略。"""

        suffix = Path(source.path).suffix.casefold()
        if suffix == ".py":
            return self._chunk_python(source)
        if suffix == ".md":
            return self._chunk_markdown(source)
        return self._chunk_text(source)

    def _drafts_from_range(
        self,
        source: RepositorySourceFile,
        *,
        kind: str,
        language: str,
        start_line: int,
        end_line: int,
        symbol: str | None = None,
        heading_path: tuple[str, ...] = (),
    ) -> tuple[RepositoryChunkDraft, ...]:
        """把一个结构范围继续按字符预算拆成带重叠的小块。"""

        lines = source.content.splitlines(keepends=True)
        if not lines:
            return ()
        start_index = max(0, start_line - 1)
        end_index = min(len(lines), end_line)
        drafts: list[RepositoryChunkDraft] = []
        cursor = start_index
        while cursor < end_index:
            current_chars = 0
            stop = cursor
            while stop < end_index:
                next_size = len(lines[stop])
                if stop > cursor and current_chars + next_size > self.config.max_chunk_chars:
                    break
                current_chars += next_size
                stop += 1
                if current_chars >= self.config.max_chunk_chars:
                    break
            if stop == cursor:
                stop += 1
            content = "".join(lines[cursor:stop]).strip()
            if content:
                actual_start = cursor + 1
                actual_end = stop
                drafts.append(
                    RepositoryChunkDraft(
                        path=source.path,
                        kind=kind,
                        language=language,
                        start_line=actual_start,
                        end_line=actual_end,
                        content=content,
                        content_hash=_sha256_text(content),
                        symbol=symbol,
                        heading_path=heading_path,
                    )
                )
            if stop >= end_index:
                break
            cursor = max(cursor + 1, stop - self.config.overlap_lines)
        return tuple(drafts)

    def _chunk_python(
        self,
        source: RepositorySourceFile,
    ) -> tuple[RepositoryChunkDraft, ...]:
        """优先按顶层类和函数切分，语法错误时退回普通文本。"""

        try:
            tree = ast.parse(source.content, filename=source.path)
        except SyntaxError:
            return self._chunk_text(source, language="python")
        lines = source.content.splitlines()
        nodes = [
            node
            for node in tree.body
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
            and getattr(node, "end_lineno", None) is not None
        ]
        drafts: list[RepositoryChunkDraft] = []
        nodes.sort(key=lambda node: node.lineno)
        first_symbol_line = min((node.lineno for node in nodes), default=len(lines) + 1)
        if first_symbol_line > 1:
            drafts.extend(
                self._drafts_from_range(
                    source,
                    kind="python_module",
                    language="python",
                    start_line=1,
                    end_line=first_symbol_line - 1,
                )
            )
        cursor = first_symbol_line
        for node in nodes:
            decorator_lines = [item.lineno for item in node.decorator_list]
            start_line = min([node.lineno, *decorator_lines])
            if cursor < start_line:
                drafts.extend(
                    self._drafts_from_range(
                        source,
                        kind="python_module",
                        language="python",
                        start_line=cursor,
                        end_line=start_line - 1,
                    )
                )
            drafts.extend(
                self._drafts_from_range(
                    source,
                    kind="python_symbol",
                    language="python",
                    start_line=start_line,
                    end_line=int(node.end_lineno),
                    symbol=node.name,
                )
            )
            cursor = int(node.end_lineno) + 1
        if cursor <= len(lines):
            drafts.extend(
                self._drafts_from_range(
                    source,
                    kind="python_module",
                    language="python",
                    start_line=cursor,
                    end_line=len(lines),
                )
            )
        if not nodes:
            return self._chunk_text(source, language="python")
        return tuple(drafts)

    def _chunk_markdown(
        self,
        source: RepositorySourceFile,
    ) -> tuple[RepositoryChunkDraft, ...]:
        """按 Markdown 标题层级建立语义段落。"""

        lines = source.content.splitlines()
        headings: list[tuple[int, int, str, tuple[str, ...]]] = []
        stack: list[str] = []
        for line_number, line in enumerate(lines, start=1):
            match = _MARKDOWN_HEADING.match(line)
            if match is None:
                continue
            level = len(match.group(1))
            title = match.group(2).strip()
            stack = stack[: level - 1]
            stack.append(title)
            headings.append((line_number, level, title, tuple(stack)))

        if not headings:
            return self._chunk_text(source, language="markdown")
        drafts: list[RepositoryChunkDraft] = []
        if headings[0][0] > 1:
            drafts.extend(
                self._drafts_from_range(
                    source,
                    kind="markdown_section",
                    language="markdown",
                    start_line=1,
                    end_line=headings[0][0] - 1,
                )
            )
        for index, (line_number, _, _, heading_path) in enumerate(headings):
            end_line = (
                headings[index + 1][0] - 1
                if index + 1 < len(headings)
                else len(lines)
            )
            drafts.extend(
                self._drafts_from_range(
                    source,
                    kind="markdown_section",
                    language="markdown",
                    start_line=line_number,
                    end_line=end_line,
                    heading_path=heading_path,
                )
            )
        return tuple(drafts)

    def _chunk_text(
        self,
        source: RepositorySourceFile,
        *,
        language: str | None = None,
    ) -> tuple[RepositoryChunkDraft, ...]:
        """普通文本按行和字符预算切分。"""

        lines = source.content.splitlines()
        if not lines:
            return ()
        return self._drafts_from_range(
            source,
            kind="text",
            language=language or Path(source.path).suffix.lstrip(".") or "text",
            start_line=1,
            end_line=len(lines),
        )
