"""只从显式可信目录发现并渐进加载 Skill。"""

from __future__ import annotations

from collections import defaultdict
import hashlib
import importlib.util
import json
from pathlib import Path, PurePosixPath
import re
import sys
from typing import Any, Iterable, Mapping

from jsonschema import SchemaError as JSONSchemaError
from jsonschema.validators import validator_for
from pydantic import ValidationError
import yaml

from repo_agent.tools.registry import ToolRegistry

from .models import (
    ActivatedSkill,
    SkillDescriptor,
    SkillDiagnostic,
    SkillDiscoveryResult,
    SkillPackageManifest,
    SkillResource,
    SkillScriptContract,
    SkillScriptDefinition,
    SkillSnapshot,
)


_SEMVER_PATTERN = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)
_MARKDOWN_LINK_PATTERN = re.compile(r"\[[^\]]+\]\(([^)]+)\)")
_TOOL_NAME_PATTERN = re.compile(r"^[a-zA-Z][a-zA-Z0-9_-]{0,99}$")


class SkillCatalogError(RuntimeError):
    """Skill 目录、元数据或激活过程不满足本地约束。"""


class SkillNotFoundError(SkillCatalogError):
    """请求的 Skill 不在最近一次有效目录快照中。"""


class SkillChangedError(SkillCatalogError):
    """发现后 Skill 元数据发生变化，需要重新扫描。"""


class SkillActivationError(SkillCatalogError):
    """Skill 与当前模式或工具权限不兼容。"""


class SkillResourceError(SkillCatalogError):
    """Skill 资源路径或内容不满足安全约束。"""


def _stable_hash(value: Any) -> str:
    """为可 JSON 化数据生成稳定 SHA-256。"""

    serialized = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _as_string_tuple(value: Any, field_name: str) -> tuple[str, ...]:
    """把扩展元数据转换为去重、保序的字符串元组。"""

    if value is None:
        return ()
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item.strip() for item in value
    ):
        raise ValueError(f"metadata.{field_name} 必须是非空字符串数组")
    return tuple(dict.fromkeys(item.strip() for item in value))


def _normalize_resource_path(value: str) -> str:
    """规范化 Skill 内部相对路径，并拒绝逃逸与 URL。"""

    normalized = value.strip().replace("\\", "/")
    if not normalized or "://" in normalized or normalized.startswith("#"):
        raise SkillResourceError(f"不是本地 Skill 资源路径：{value}")
    path = PurePosixPath(normalized)
    if path.is_absolute() or ".." in path.parts or "." in path.parts:
        raise SkillResourceError(f"Skill 资源路径越界：{value}")
    if not path.parts or path.parts[0] not in {"references", "assets"}:
        raise SkillResourceError(
            f"只允许按需读取 references/ 或 assets/：{value}"
        )
    return path.as_posix()


def _normalize_package_path(
    value: str,
    allowed_roots: set[str],
) -> str:
    """校验能力包中脚本、Schema、资源和测试的相对路径。"""

    normalized = value.strip().replace("\\", "/")
    path = PurePosixPath(normalized)
    if (
        not normalized
        or "://" in normalized
        or path.is_absolute()
        or ".." in path.parts
        or "." in path.parts
        or not path.parts
        or path.parts[0] not in allowed_roots
    ):
        raise SkillResourceError(f"Skill 能力包路径越界：{value}")
    return path.as_posix()


def _split_allowed_tools(value: Any) -> tuple[str, ...]:
    """解析开放规范中的空格分隔 allowed-tools 字段。"""

    if value is None:
        return ()
    if not isinstance(value, str):
        raise ValueError("allowed-tools 必须是空格分隔字符串")
    tools = tuple(dict.fromkeys(item for item in value.split() if item))
    invalid = [item for item in tools if not _TOOL_NAME_PATTERN.fullmatch(item)]
    if invalid:
        raise ValueError(f"allowed-tools 包含不受支持的工具名：{invalid}")
    return tools


def _read_frontmatter(path: Path, max_bytes: int) -> tuple[Mapping[str, Any], bytes]:
    """只读取 YAML 头，不在发现阶段加载 Skill 正文。"""

    consumed = 0
    lines: list[bytes] = []
    with path.open("rb") as handle:
        first = handle.readline()
        consumed += len(first)
        if first.rstrip(b"\r\n") != b"---":
            raise ValueError("SKILL.md 必须以 YAML frontmatter 开始")
        while True:
            line = handle.readline()
            if not line:
                raise ValueError("SKILL.md 缺少 frontmatter 结束标记")
            consumed += len(line)
            if consumed > max_bytes:
                raise ValueError("Skill frontmatter 超过大小上限")
            if line.rstrip(b"\r\n") == b"---":
                break
            lines.append(line)
    raw_header = b"".join(lines)
    try:
        loaded = yaml.safe_load(raw_header.decode("utf-8")) or {}
    except (UnicodeDecodeError, yaml.YAMLError) as exc:
        raise ValueError(f"Skill frontmatter 无法解析：{exc}") from exc
    if not isinstance(loaded, Mapping):
        raise ValueError("Skill frontmatter 必须是对象")
    return loaded, raw_header


def _read_yaml_object(path: Path, max_bytes: int) -> Mapping[str, Any]:
    """读取有限大小的 UTF-8 YAML 对象。"""

    raw = path.read_bytes()
    if len(raw) > max_bytes:
        raise ValueError(f"{path.name} 超过大小上限")
    try:
        loaded = yaml.safe_load(raw.decode("utf-8")) or {}
    except (UnicodeDecodeError, yaml.YAMLError) as exc:
        raise ValueError(f"{path.name} 无法解析：{exc}") from exc
    if not isinstance(loaded, Mapping):
        raise ValueError(f"{path.name} 必须是对象")
    return loaded


def _build_descriptor(
    skill_root: Path,
    skill_file: Path,
    header: Mapping[str, Any],
    *,
    manifest_file: Path | None = None,
    manifest_data: Mapping[str, Any] | None = None,
) -> SkillDescriptor:
    """校验开放规范字段和 RepoAgent 扩展字段。"""

    name = header.get("name")
    description = header.get("description")
    if not isinstance(name, str) or not isinstance(description, str):
        raise ValueError("Skill 必须包含字符串 name 和 description")
    if skill_root.name != name:
        raise ValueError("Skill name 必须与父目录名一致")

    if manifest_data is not None:
        unexpected = sorted(set(header) - {"name", "description"})
        if unexpected:
            raise ValueError(
                "Skill v2 的 SKILL.md frontmatter 只允许 name 和 description："
                + ", ".join(unexpected)
            )
        package = SkillPackageManifest.model_validate(manifest_data)
        if not _SEMVER_PATTERN.fullmatch(package.version):
            raise ValueError("skill.yaml version 必须是完整 SemVer，例如 2.0.0")
        instruction_resources = tuple(
            _normalize_package_path(item, {"references"})
            for item in package.instruction_resources
        )
        assets = tuple(
            _normalize_package_path(item, {"assets"}) for item in package.assets
        )
        tests = tuple(
            _normalize_package_path(item, {"tests"}) for item in package.tests
        )
        scripts: list[SkillScriptDefinition] = []
        for script in package.scripts:
            scripts.append(
                script.model_copy(
                    update={
                        "path": _normalize_package_path(script.path, {"scripts"}),
                        "input_schema": _normalize_package_path(
                            script.input_schema,
                            {"schemas"},
                        ),
                        "output_schema": _normalize_package_path(
                            script.output_schema,
                            {"schemas"},
                        ),
                    }
                )
            )
        script_names = [script.tool_name for script in scripts]
        if len(script_names) != len(set(script_names)):
            raise ValueError("skill.yaml scripts.tool_name 不能重复")
        allowed_tools = tuple(
            dict.fromkeys([*package.allowed_tools, *script_names])
        )
        invalid_tools = [
            item for item in allowed_tools if not _TOOL_NAME_PATTERN.fullmatch(item)
        ]
        if invalid_tools:
            raise ValueError(f"skill.yaml 包含非法工具名：{invalid_tools}")
        required_tools = package.required_tools
        if not set(required_tools).issubset(allowed_tools):
            raise ValueError("required_tools 必须是 allowed_tools 的子集")
        declared_resources = tuple(
            dict.fromkeys(
                [
                    *instruction_resources,
                    *assets,
                    *tests,
                    *(script.path for script in scripts),
                    *(script.input_schema for script in scripts),
                    *(script.output_schema for script in scripts),
                ]
            )
        )
        descriptor_payload = {
            "name": name,
            "description": description,
            "version": package.version,
            "allowed_tools": allowed_tools,
            "required_tools": required_tools,
            "modes": package.modes,
            "tags": package.tags,
            "triggers": package.triggers,
            "declared_resources": declared_resources,
            "instruction_resources": instruction_resources,
            "assets": assets,
            "tests": tests,
            "dependencies": package.dependencies.model_dump(mode="json"),
            "scripts": [script.model_dump(mode="json") for script in scripts],
        }
        return SkillDescriptor(
            **descriptor_payload,
            skill_root=skill_root,
            skill_file=skill_file,
            manifest_file=manifest_file,
            package_format="v2",
            metadata_hash=_stable_hash(descriptor_payload),
        )

    metadata = header.get("metadata") or {}
    if not isinstance(metadata, Mapping):
        raise ValueError("metadata 必须是对象")
    version = metadata.get("version", "0.0.0")
    if not isinstance(version, str) or not _SEMVER_PATTERN.fullmatch(version):
        raise ValueError("metadata.version 必须是完整 SemVer，例如 1.0.0")

    modes = _as_string_tuple(metadata.get("modes"), "modes") or (
        "diagnose",
        "fix",
    )
    if any(mode not in {"diagnose", "fix"} for mode in modes):
        raise ValueError("metadata.modes 只允许 diagnose 和 fix")

    allowed_tools = _split_allowed_tools(header.get("allowed-tools"))
    required_tools = _as_string_tuple(
        metadata.get("required-tools", metadata.get("required_tools")),
        "required-tools",
    )
    if allowed_tools and not set(required_tools).issubset(allowed_tools):
        raise ValueError("required-tools 必须是 allowed-tools 的子集")

    resources = tuple(
        _normalize_resource_path(item)
        for item in _as_string_tuple(metadata.get("resources"), "resources")
    )
    descriptor_payload = {
        "name": name,
        "description": description,
        "version": version,
        "allowed_tools": allowed_tools,
        "required_tools": required_tools,
        "modes": modes,
        "tags": _as_string_tuple(metadata.get("tags"), "tags"),
        "triggers": _as_string_tuple(metadata.get("triggers"), "triggers"),
        "declared_resources": resources,
    }
    return SkillDescriptor(
        **descriptor_payload,
        skill_root=skill_root,
        skill_file=skill_file,
        package_format="legacy",
        metadata_hash=_stable_hash(descriptor_payload),
    )


def _is_symlink_in_tree(path: Path, stop: Path) -> bool:
    """检查从资源到 Skill 根之间是否经过符号链接。"""

    current = path
    while True:
        if current.is_symlink():
            return True
        if current == stop:
            return False
        if stop not in current.parents:
            return True
        current = current.parent


class SkillCatalog:
    """维护来自显式可信根目录的 Skill 元数据快照。"""

    def __init__(
        self,
        trusted_roots: Iterable[Path | str],
        *,
        max_frontmatter_bytes: int = 32_768,
        max_skill_bytes: int = 256_000,
        max_resource_bytes: int = 128_000,
    ) -> None:
        roots = tuple(Path(root).expanduser().absolute() for root in trusted_roots)
        if not roots:
            raise ValueError("至少需要一个显式 trusted Skill 根目录")
        if max_frontmatter_bytes < 1_024:
            raise ValueError("max_frontmatter_bytes 不能小于 1024")
        self.trusted_roots = roots
        self.max_frontmatter_bytes = max_frontmatter_bytes
        self.max_skill_bytes = max_skill_bytes
        self.max_resource_bytes = max_resource_bytes
        self._skills: dict[str, SkillDescriptor] = {}
        self._last_result = SkillDiscoveryResult(skills=(), diagnostics=())

    @property
    def last_result(self) -> SkillDiscoveryResult:
        """返回最近一次扫描结果。"""

        return self._last_result

    def refresh(self) -> SkillDiscoveryResult:
        """扫描一层 Skill 目录，跳过非法项并保留诊断。"""

        found: list[SkillDescriptor] = []
        diagnostics: list[SkillDiagnostic] = []
        for trusted_root in self.trusted_roots:
            if not trusted_root.exists() or not trusted_root.is_dir():
                diagnostics.append(
                    SkillDiagnostic(
                        path=trusted_root,
                        level="error",
                        code="trusted_root_missing",
                        message="显式可信 Skill 根目录不存在或不是目录",
                    )
                )
                continue
            candidates = (
                (trusted_root,)
                if (trusted_root / "SKILL.md").is_file()
                else tuple(
                    path
                    for path in sorted(trusted_root.iterdir(), key=lambda item: item.name)
                    if path.is_dir() or path.is_symlink()
                )
            )
            for skill_root in candidates:
                skill_file = skill_root / "SKILL.md"
                try:
                    if _is_symlink_in_tree(skill_root, trusted_root):
                        raise ValueError("Skill 目录不能是符号链接")
                    if not skill_file.is_file() or skill_file.is_symlink():
                        raise ValueError("Skill 目录缺少普通文件 SKILL.md")
                    header, _ = _read_frontmatter(
                        skill_file,
                        self.max_frontmatter_bytes,
                    )
                    manifest_file = skill_root / "skill.yaml"
                    if manifest_file.is_symlink():
                        raise ValueError("skill.yaml 不能是符号链接")
                    manifest_data = (
                        _read_yaml_object(
                            manifest_file,
                            self.max_frontmatter_bytes,
                        )
                        if manifest_file.is_file() and not manifest_file.is_symlink()
                        else None
                    )
                    found.append(
                        _build_descriptor(
                            skill_root,
                            skill_file,
                            header,
                            manifest_file=(
                                manifest_file if manifest_data is not None else None
                            ),
                            manifest_data=manifest_data,
                        )
                    )
                except (OSError, ValueError, ValidationError, SkillResourceError) as exc:
                    diagnostics.append(
                        SkillDiagnostic(
                            path=skill_root,
                            level="error",
                            code="invalid_skill",
                            message=str(exc),
                        )
                    )

        by_name: dict[str, list[SkillDescriptor]] = defaultdict(list)
        for descriptor in found:
            by_name[descriptor.name].append(descriptor)
        valid: list[SkillDescriptor] = []
        for name, descriptors in sorted(by_name.items()):
            if len(descriptors) == 1:
                valid.append(descriptors[0])
                continue
            for descriptor in descriptors:
                diagnostics.append(
                    SkillDiagnostic(
                        path=descriptor.skill_root,
                        level="error",
                        code="duplicate_skill_name",
                        message=f"多个可信根目录声明了同名 Skill：{name}",
                    )
                )

        valid.sort(key=lambda item: item.name)
        diagnostics.sort(key=lambda item: (str(item.path), item.code))
        self._skills = {item.name: item for item in valid}
        self._last_result = SkillDiscoveryResult(
            skills=tuple(valid),
            diagnostics=tuple(diagnostics),
        )
        return self._last_result

    def descriptors(self) -> tuple[SkillDescriptor, ...]:
        """返回最近一次扫描后的稳定排序目录。"""

        return tuple(self._skills[name] for name in sorted(self._skills))

    def get(self, name: str) -> SkillDescriptor:
        """按名称取得已发现 Skill。"""

        try:
            return self._skills[name]
        except KeyError as exc:
            raise SkillNotFoundError(f"未发现 Skill：{name}") from exc

    def _load_full_skill(
        self,
        descriptor: SkillDescriptor,
    ) -> tuple[SkillDescriptor, str, str]:
        try:
            raw = descriptor.skill_file.read_bytes()
        except OSError as exc:
            raise SkillActivationError(f"无法读取 Skill：{exc}") from exc
        if len(raw) > self.max_skill_bytes:
            raise SkillActivationError("SKILL.md 超过激活大小上限")
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SkillActivationError("SKILL.md 必须使用 UTF-8") from exc
        lines = text.splitlines()
        try:
            closing = lines.index("---", 1)
        except ValueError as exc:
            raise SkillActivationError("SKILL.md frontmatter 未闭合") from exc
        try:
            header = yaml.safe_load("\n".join(lines[1:closing])) or {}
            manifest_file = descriptor.skill_root / "skill.yaml"
            manifest_data = (
                _read_yaml_object(manifest_file, self.max_frontmatter_bytes)
                if descriptor.package_format == "v2"
                else None
            )
            current = _build_descriptor(
                descriptor.skill_root,
                descriptor.skill_file,
                header,
                manifest_file=(
                    manifest_file if descriptor.package_format == "v2" else None
                ),
                manifest_data=manifest_data,
            )
        except (yaml.YAMLError, ValueError, ValidationError, SkillResourceError) as exc:
            raise SkillActivationError(f"Skill 元数据失效：{exc}") from exc
        if current.metadata_hash != descriptor.metadata_hash:
            raise SkillChangedError("Skill 元数据在发现后发生变化，请重新 refresh")
        body = "\n".join(lines[closing + 1 :]).strip()
        if not body:
            raise SkillActivationError("Skill 正文不能为空")
        self._validate_dependencies(current)
        return current, body, self._package_hash(current, raw)

    def _resolve_declared_file(
        self,
        descriptor: SkillDescriptor,
        relative_path: str,
    ) -> Path:
        """解析已声明文件并拒绝符号链接、目录和路径逃逸。"""

        root = descriptor.skill_root
        target = root.joinpath(*PurePosixPath(relative_path).parts)
        if _is_symlink_in_tree(target, root):
            raise SkillResourceError("Skill 能力包文件路径经过符号链接")
        if not target.is_file():
            raise SkillResourceError(f"Skill 能力包文件不存在：{relative_path}")
        return target

    def _package_hash(self, descriptor: SkillDescriptor, skill_raw: bytes) -> str:
        """计算覆盖入口、Manifest 和全部声明文件的能力包哈希。"""

        hashes: dict[str, str] = {
            "SKILL.md": hashlib.sha256(skill_raw).hexdigest(),
        }
        if descriptor.manifest_file is not None:
            manifest_raw = descriptor.manifest_file.read_bytes()
            hashes["skill.yaml"] = hashlib.sha256(manifest_raw).hexdigest()
        for relative in descriptor.declared_resources:
            target = self._resolve_declared_file(descriptor, relative)
            raw = target.read_bytes()
            if len(raw) > self.max_resource_bytes:
                raise SkillActivationError(
                    f"Skill 能力包文件超过大小上限：{relative}"
                )
            hashes[relative] = hashlib.sha256(raw).hexdigest()
        agents_file = descriptor.skill_root / "agents" / "openai.yaml"
        if agents_file.exists():
            if _is_symlink_in_tree(agents_file, descriptor.skill_root):
                raise SkillActivationError("agents/openai.yaml 不能经过符号链接")
            hashes["agents/openai.yaml"] = hashlib.sha256(
                agents_file.read_bytes()
            ).hexdigest()
        return _stable_hash(hashes)

    @staticmethod
    def _validate_dependencies(descriptor: SkillDescriptor) -> None:
        """激活前验证 Python 下限和可导入依赖，不执行安装。"""

        requirement = descriptor.dependencies.python
        if requirement is not None:
            major, minor = (
                int(item) for item in requirement.removeprefix(">=").split(".")
            )
            if sys.version_info < (major, minor):
                raise SkillActivationError(
                    f"Skill {descriptor.name} 需要 Python {requirement}"
                )
        missing = sorted(
            package
            for package in descriptor.dependencies.packages
            if importlib.util.find_spec(package) is None
        )
        if missing:
            raise SkillActivationError(
                "Skill 缺少本地依赖，宿主不会自动安装：" + ", ".join(missing)
            )

    def _read_text_resource(
        self,
        descriptor: SkillDescriptor,
        relative_path: str,
    ) -> SkillResource:
        """读取一个已经声明的有限 UTF-8 文本资源。"""

        target = self._resolve_declared_file(descriptor, relative_path)
        if target.suffix.casefold() not in {
            ".md",
            ".txt",
            ".json",
            ".yaml",
            ".yml",
        }:
            raise SkillResourceError("只允许读取文本型 Skill 资源")
        raw = target.read_bytes()
        if len(raw) > self.max_resource_bytes:
            raise SkillResourceError("Skill 资源超过大小上限")
        try:
            content = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SkillResourceError("Skill 资源必须使用 UTF-8") from exc
        return SkillResource(
            skill_name=descriptor.name,
            relative_path=relative_path,
            content=content,
            content_hash=hashlib.sha256(raw).hexdigest(),
        )

    def activate(
        self,
        name: str,
        tool_registry: ToolRegistry,
        *,
        runtime_allowed_tools: Iterable[str] | None = None,
        mode: str | None = None,
    ) -> ActivatedSkill:
        """加载正文，并把 Skill 工具声明与运行时权限做交集。"""

        descriptor = self.get(name)
        current, body, package_hash = self._load_full_skill(descriptor)
        if mode is not None and mode not in current.modes:
            raise SkillActivationError(
                f"Skill {name} 不支持当前模式 {mode}，支持：{current.modes}"
            )

        registered = {tool.name for tool in tool_registry.model_tools()}
        runtime_cap = (
            registered
            if runtime_allowed_tools is None
            else registered.intersection(runtime_allowed_tools)
        )
        effective = (
            runtime_cap.intersection(current.allowed_tools)
            if current.allowed_tools
            else runtime_cap
        )
        missing_required = sorted(set(current.required_tools) - registered)
        if missing_required:
            raise SkillActivationError(
                "Skill 运行环境缺少必需工具："
                + ", ".join(missing_required)
            )

        references = list(current.declared_resources)
        for target in _MARKDOWN_LINK_PATTERN.findall(body):
            try:
                normalized = _normalize_resource_path(target)
            except SkillResourceError:
                continue
            if normalized not in references:
                references.append(normalized)
        loaded_resources = tuple(
            self._read_text_resource(current, relative)
            for relative in current.instruction_resources
        )
        return ActivatedSkill(
            descriptor=current,
            instructions=body,
            content_hash=package_hash,
            effective_tools=tuple(sorted(effective)),
            references=tuple(references),
            loaded_resources=loaded_resources,
            scripts=current.scripts,
        )

    def validate_snapshot(
        self,
        snapshot: SkillSnapshot,
        tool_registry: ToolRegistry,
        *,
        runtime_allowed_tools: Iterable[str] | None = None,
        mode: str | None = None,
    ) -> ActivatedSkill:
        """恢复运行前重新加载 Skill，防止静默漂移。"""

        activated = self.activate(
            snapshot.name,
            tool_registry,
            runtime_allowed_tools=runtime_allowed_tools,
            mode=mode,
        )
        if activated.descriptor.version != snapshot.version:
            raise SkillChangedError("Skill 版本与 Checkpoint 快照不一致")
        if activated.content_hash != snapshot.content_hash:
            raise SkillChangedError("Skill 内容哈希与 Checkpoint 快照不一致")
        return activated

    def load_resource(
        self,
        activated: ActivatedSkill,
        relative_path: str,
    ) -> SkillResource:
        """只按需读取正文已引用或元数据已声明的文本资源。"""

        normalized = _normalize_resource_path(relative_path)
        if normalized not in activated.references:
            raise SkillResourceError("资源未被 Skill 正文引用或元数据声明")
        return self._read_text_resource(activated.descriptor, normalized)

    def load_script_contract(
        self,
        descriptor: SkillDescriptor,
        script: SkillScriptDefinition,
    ) -> SkillScriptContract:
        """加载脚本与输入输出 Schema，并验证 Schema 本身合法。"""

        if script not in descriptor.scripts:
            raise SkillResourceError("脚本不属于当前 Skill 描述快照")
        script_path = self._resolve_declared_file(descriptor, script.path)
        if script_path.suffix.casefold() != ".py":
            raise SkillResourceError("当前只支持 Python Skill Script")

        schemas: list[dict[str, Any]] = []
        schema_hashes: list[str] = []
        for relative in (script.input_schema, script.output_schema):
            target = self._resolve_declared_file(descriptor, relative)
            raw = target.read_bytes()
            if len(raw) > self.max_resource_bytes:
                raise SkillResourceError(f"Skill Schema 超过大小上限：{relative}")
            try:
                loaded = json.loads(raw.decode("utf-8"))
                if not isinstance(loaded, dict):
                    raise ValueError("Schema 顶层必须是对象")
                validator_for(loaded).check_schema(loaded)
            except (
                UnicodeDecodeError,
                json.JSONDecodeError,
                ValueError,
                JSONSchemaError,
            ) as exc:
                raise SkillResourceError(f"Skill Schema 非法：{relative}") from exc
            schemas.append(loaded)
            schema_hashes.append(hashlib.sha256(raw).hexdigest())
        script_hash = hashlib.sha256(script_path.read_bytes()).hexdigest()
        return SkillScriptContract(
            skill_name=descriptor.name,
            skill_root=descriptor.skill_root,
            definition=script,
            script_path=script_path,
            input_schema=schemas[0],
            output_schema=schemas[1],
            contract_hash=_stable_hash(
                {
                    "script": script_hash,
                    "input_schema": schema_hashes[0],
                    "output_schema": schema_hashes[1],
                }
            ),
        )
