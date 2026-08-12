"""把可信 Skill 中声明的确定性脚本映射为受控 Tool。"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any, Iterator, Mapping

from jsonschema import ValidationError as JSONValidationError
from jsonschema.validators import validator_for

from repo_agent.tools import ToolDefinition, ToolErrorKind, ToolRegistry, ToolResult

from .catalog import SkillCatalog
from .models import SkillScriptContract


_ACTIVE_SKILL: ContextVar[str | None] = ContextVar(
    "repo_agent_active_skill",
    default=None,
)


@contextmanager
def skill_script_scope(skill_name: str | None) -> Iterator[None]:
    """限制脚本工具只能在对应 Skill 已激活的同步调用链中运行。"""

    token = _ACTIVE_SKILL.set(skill_name)
    try:
        yield
    finally:
        _ACTIVE_SKILL.reset(token)


def _isolated_environment() -> dict[str, str]:
    """构造不携带模型密钥和业务密钥的最小子进程环境。"""

    environment = {
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUTF8": "1",
    }
    for name in ("SYSTEMROOT", "WINDIR", "TEMP", "TMP"):
        value = os.environ.get(name)
        if value:
            environment[name] = value
    return environment


def _bounded(value: str, limit: int) -> tuple[str, bool]:
    """保留脚本输出头尾，并明确标记截断。"""

    if len(value) <= limit:
        return value, False
    marker = "\n...<Skill Script 输出已截断>...\n"
    remaining = max(0, limit - len(marker))
    head = remaining // 2
    return value[:head] + marker + value[-(remaining - head) :], True


class SkillScriptExecutor:
    """使用 JSON stdin/stdout、Schema 和有限子进程运行脚本。"""

    def __init__(
        self,
        contract: SkillScriptContract,
        *,
        allow_explicit_execution: bool = False,
    ) -> None:
        self.contract = contract
        self.allow_explicit_execution = allow_explicit_execution
        validator_type = validator_for(contract.output_schema)
        self.output_validator = validator_type(contract.output_schema)

    def execute(self, arguments: Mapping[str, Any]) -> ToolResult[dict[str, Any]]:
        """校验作用域后执行脚本，并把合法 JSON 输出转换为工具观察。"""

        definition = self.contract.definition
        if _ACTIVE_SKILL.get() != self.contract.skill_name:
            return ToolResult.failure(
                ToolErrorKind.PERMISSION_DENIED,
                f"脚本工具只能在 Skill {self.contract.skill_name} 激活后调用",
            )
        if (
            definition.requires_explicit_authorization
            and not self.allow_explicit_execution
        ):
            return ToolResult.failure(
                ToolErrorKind.PERMISSION_DENIED,
                "Skill Script 需要显式执行授权",
            )
        payload = json.dumps(
            dict(arguments),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        if len(payload) > 200_000:
            return ToolResult.failure(
                ToolErrorKind.INVALID_ARGUMENT,
                "Skill Script 输入超过 200000 字符上限",
            )

        started = time.perf_counter()
        try:
            completed = subprocess.run(
                (
                    sys.executable,
                    "-I",
                    "-X",
                    "utf8",
                    str(self.contract.script_path),
                ),
                cwd=self.contract.skill_root,
                input=payload,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=definition.timeout_seconds,
                shell=False,
                env=_isolated_environment(),
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            return ToolResult.failure(
                ToolErrorKind.TIMEOUT,
                f"Skill Script 执行超过 {definition.timeout_seconds} 秒",
                retryable=True,
                details={"stderr": str(exc.stderr or "")[:2_000]},
            )
        except OSError as exc:
            return ToolResult.failure(
                ToolErrorKind.EXECUTION_ERROR,
                "Skill Script 无法启动",
                details={"exception_type": type(exc).__name__, "message": str(exc)},
            )

        duration_ms = int((time.perf_counter() - started) * 1_000)
        stdout, stdout_truncated = _bounded(
            completed.stdout,
            definition.max_output_chars,
        )
        stderr, stderr_truncated = _bounded(completed.stderr, 10_000)
        if completed.returncode != 0:
            return ToolResult.failure(
                ToolErrorKind.EXECUTION_ERROR,
                f"Skill Script 返回非零退出码：{completed.returncode}",
                details={"stderr": stderr, "stdout": stdout},
                metadata={"duration_ms": duration_ms},
            )
        if stdout_truncated:
            return ToolResult.failure(
                ToolErrorKind.PARSE_ERROR,
                "Skill Script JSON 输出超过上限，拒绝解析截断结果",
                details={"stderr": stderr},
                metadata={"duration_ms": duration_ms},
            )
        try:
            result = json.loads(stdout)
            if not isinstance(result, dict):
                raise ValueError("输出顶层必须是 JSON 对象")
            self.output_validator.validate(result)
        except (json.JSONDecodeError, JSONValidationError, ValueError) as exc:
            return ToolResult.failure(
                ToolErrorKind.PARSE_ERROR,
                "Skill Script 输出不满足 JSON Schema",
                details={
                    "error": str(exc),
                    "stderr": stderr,
                },
                metadata={"duration_ms": duration_ms},
            )
        return ToolResult.success(
            result,
            metadata={
                "skill_name": self.contract.skill_name,
                "script_tool": definition.tool_name,
                "contract_hash": self.contract.contract_hash,
                "duration_ms": duration_ms,
                "stderr": stderr,
                "output_truncated": stdout_truncated or stderr_truncated,
            },
        )


def register_skill_script_tools(
    catalog: SkillCatalog,
    registry: ToolRegistry,
    *,
    allow_explicit_execution: bool = False,
) -> tuple[str, ...]:
    """把所有已发现能力包脚本注册为受 Skill 作用域保护的工具。"""

    registered: list[str] = []
    for descriptor in catalog.descriptors():
        for script in descriptor.scripts:
            contract = catalog.load_script_contract(descriptor, script)
            executor = SkillScriptExecutor(
                contract,
                allow_explicit_execution=allow_explicit_execution,
            )
            registry.register_json_schema(
                ToolDefinition(
                    name=script.tool_name,
                    description=(
                        f"Skill {descriptor.name} 的确定性脚本：{script.description}"
                    ),
                    access=script.access,
                    executes_project_code=script.executes_project_code,
                    requires_explicit_authorization=(
                        script.requires_explicit_authorization
                    ),
                ),
                contract.input_schema,
                executor.execute,
            )
            registered.append(script.tool_name)
    return tuple(registered)
