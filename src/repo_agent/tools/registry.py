"""模型可发现、可校验、可分发的工具注册表。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping

from jsonschema import SchemaError as JSONSchemaError
from jsonschema import ValidationError as JSONValidationError
from jsonschema.validators import validator_for
from pydantic import BaseModel, ValidationError

from .arguments import (
    InspectPythonArguments,
    ListFilesArguments,
    ReadFileRangeArguments,
    RunPytestArguments,
    SearchCodeArguments,
)
from .catalog import REPOSITORY_TOOL_CATALOG, ToolDefinition
from .models import ToolErrorKind, ToolResult
from .repository import LocalRepositoryTools


ToolHandler = Callable[[Any], ToolResult[Any]]
ToolArgumentValidator = Callable[[Mapping[str, Any]], Any]


@dataclass(frozen=True, slots=True)
class ModelToolDefinition:
    """交给模型的工具说明和 JSON Schema。"""

    name: str
    description: str
    input_schema: dict[str, Any]
    access: str
    executes_project_code: bool
    requires_explicit_authorization: bool


@dataclass(frozen=True, slots=True)
class RegisteredTool:
    """注册表内部保存的工具参数模型和处理函数。"""

    definition: ToolDefinition
    input_schema: dict[str, Any]
    argument_validator: ToolArgumentValidator
    handler: ToolHandler


class ToolRegistry:
    """负责工具发现、参数验证、权限过滤和调用分发。"""

    def __init__(self) -> None:
        self._tools: dict[str, RegisteredTool] = {}

    def register(
        self,
        definition: ToolDefinition,
        argument_model: type[BaseModel],
        handler: ToolHandler,
    ) -> None:
        """注册一个工具，拒绝名称覆盖。"""

        if definition.name in self._tools:
            raise ValueError(f"工具已注册：{definition.name}")
        self._tools[definition.name] = RegisteredTool(
            definition=definition,
            input_schema=argument_model.model_json_schema(),
            argument_validator=argument_model.model_validate,
            handler=handler,
        )

    def register_json_schema(
        self,
        definition: ToolDefinition,
        input_schema: Mapping[str, Any],
        handler: ToolHandler,
    ) -> None:
        """注册外部 JSON Schema 工具，并在真正调用前再次校验参数。"""

        if definition.name in self._tools:
            raise ValueError(f"工具已注册：{definition.name}")
        schema = dict(input_schema)
        try:
            validator_type = validator_for(schema)
            validator_type.check_schema(schema)
            validator = validator_type(schema)
        except JSONSchemaError as exc:
            raise ValueError(f"工具 JSON Schema 非法：{definition.name}") from exc

        def validate(arguments: Mapping[str, Any]) -> dict[str, Any]:
            """返回与模型参数隔离的普通字典副本。"""

            candidate = dict(arguments)
            validator.validate(candidate)
            return candidate

        self._tools[definition.name] = RegisteredTool(
            definition=definition,
            input_schema=schema,
            argument_validator=validate,
            handler=handler,
        )

    def model_tools(
        self,
        allowed_tools: Iterable[str] | None = None,
    ) -> tuple[ModelToolDefinition, ...]:
        """返回当前任务允许暴露给模型的工具定义。"""

        allowed = set(allowed_tools) if allowed_tools is not None else None
        definitions: list[ModelToolDefinition] = []
        for name in sorted(self._tools):
            if allowed is not None and name not in allowed:
                continue
            tool = self._tools[name]
            definitions.append(
                ModelToolDefinition(
                    name=name,
                    description=tool.definition.description,
                    input_schema=dict(tool.input_schema),
                    access=tool.definition.access,
                    executes_project_code=tool.definition.executes_project_code,
                    requires_explicit_authorization=(
                        tool.definition.requires_explicit_authorization
                    ),
                )
            )
        return tuple(definitions)

    def dispatch(
        self,
        tool_name: str,
        raw_arguments: Mapping[str, Any],
        *,
        allowed_tools: Iterable[str] | None = None,
    ) -> ToolResult[Any]:
        """校验模型参数并调用已注册工具。"""

        tool = self._tools.get(tool_name)
        if tool is None:
            return ToolResult.failure(
                ToolErrorKind.NOT_FOUND,
                f"未知工具：{tool_name}",
                details={"available_tools": sorted(self._tools)},
            )

        if allowed_tools is not None and tool_name not in set(allowed_tools):
            return ToolResult.failure(
                ToolErrorKind.PERMISSION_DENIED,
                f"当前任务不允许调用工具：{tool_name}",
            )

        try:
            arguments = tool.argument_validator(dict(raw_arguments))
        except (
            ValidationError,
            JSONValidationError,
            TypeError,
            ValueError,
        ) as exc:
            details: dict[str, Any]
            if isinstance(exc, ValidationError):
                details = {"errors": exc.errors(include_url=False)}
            elif isinstance(exc, JSONValidationError):
                details = {
                    "path": [str(item) for item in exc.absolute_path],
                    "validator": exc.validator,
                    "message": exc.message,
                }
            else:
                details = {"error": str(exc)}
            return ToolResult.failure(
                ToolErrorKind.INVALID_ARGUMENT,
                f"工具参数校验失败：{tool_name}",
                details=details,
            )

        try:
            return tool.handler(arguments)
        except Exception as exc:
            return ToolResult.failure(
                ToolErrorKind.INTERNAL_ERROR,
                f"工具处理函数出现未预期错误：{tool_name}",
                details={"exception_type": type(exc).__name__, "message": str(exc)},
            )


def build_repository_tool_registry(tools: LocalRepositoryTools) -> ToolRegistry:
    """将本地仓库工具注册到模型工具边界。"""

    registry = ToolRegistry()
    registry.register(
        REPOSITORY_TOOL_CATALOG["list_files"],
        ListFilesArguments,
        lambda arguments: tools.list_files(arguments.to_request()),
    )
    registry.register(
        REPOSITORY_TOOL_CATALOG["search_code"],
        SearchCodeArguments,
        lambda arguments: tools.search_code(arguments.to_request()),
    )
    registry.register(
        REPOSITORY_TOOL_CATALOG["read_file_range"],
        ReadFileRangeArguments,
        lambda arguments: tools.read_file_range(arguments.to_request()),
    )
    registry.register(
        REPOSITORY_TOOL_CATALOG["inspect_python"],
        InspectPythonArguments,
        lambda arguments: tools.inspect_python(arguments.to_request()),
    )
    registry.register(
        REPOSITORY_TOOL_CATALOG["run_pytest"],
        RunPytestArguments,
        lambda arguments: tools.run_pytest(arguments.to_request()),
    )
    return registry
