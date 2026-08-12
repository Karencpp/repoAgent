"""供工具注册、权限判断和模型描述使用的能力目录。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    """一个工具的稳定能力与风险元数据。"""

    name: str
    description: str
    access: Literal["read", "execute"]
    executes_project_code: bool
    requires_explicit_authorization: bool


REPOSITORY_TOOL_DEFINITIONS = (
    ToolDefinition(
        name="list_files",
        description="列出目标代码库中受控深度和数量的目录项。",
        access="read",
        executes_project_code=False,
        requires_explicit_authorization=False,
    ),
    ToolDefinition(
        name="search_code",
        description="在有限大小的文本文件中执行精确字符串搜索。",
        access="read",
        executes_project_code=False,
        requires_explicit_authorization=False,
    ),
    ToolDefinition(
        name="read_file_range",
        description="按行号读取目标代码库中文本文件的有限片段。",
        access="read",
        executes_project_code=False,
        requires_explicit_authorization=False,
    ),
    ToolDefinition(
        name="inspect_python",
        description="通过 AST 获取 Python 模块导入和符号结构，不执行目标代码。",
        access="read",
        executes_project_code=False,
        requires_explicit_authorization=False,
    ),
    ToolDefinition(
        name="run_pytest",
        description="在固定代码库目录内运行受限 pytest，并返回退出码和输出。",
        access="execute",
        executes_project_code=True,
        requires_explicit_authorization=True,
    ),
)


REPOSITORY_TOOL_CATALOG = {
    definition.name: definition for definition in REPOSITORY_TOOL_DEFINITIONS
}

