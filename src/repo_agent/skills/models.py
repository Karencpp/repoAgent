"""Skill 发现、路由、激活和资源读取使用的领域模型。"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class SkillModel(BaseModel):
    """Skill 模块统一使用严格且不可变的数据模型。"""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


class SkillDependencies(SkillModel):
    """能力包激活前需要验证的本地运行依赖。"""

    python: str | None = Field(
        default=None,
        pattern=r"^>=(?:3|4)\.\d{1,2}$",
    )
    packages: tuple[str, ...] = Field(default=(), max_length=50)


class SkillScriptDefinition(SkillModel):
    """映射为受控 Tool 的确定性 Skill 脚本。"""

    tool_name: str = Field(
        alias="name",
        pattern=r"^[a-zA-Z][a-zA-Z0-9_-]{0,99}$",
    )
    description: str = Field(min_length=1, max_length=1_000)
    path: str = Field(min_length=1, max_length=500)
    input_schema: str = Field(min_length=1, max_length=500)
    output_schema: str = Field(min_length=1, max_length=500)
    timeout_seconds: float = Field(default=5.0, ge=0.1, le=30.0)
    max_output_chars: int = Field(default=50_000, ge=1_000, le=200_000)
    access: Literal["read", "execute"] = "read"
    executes_project_code: bool = False
    requires_explicit_authorization: bool = False


class SkillPackageManifest(SkillModel):
    """SKILL.md 之外的 RepoAgent 能力包声明。"""

    format_version: Literal[2] = 2
    version: str
    modes: tuple[Literal["diagnose", "fix"], ...] = ("diagnose", "fix")
    tags: tuple[str, ...] = ()
    triggers: tuple[str, ...] = ()
    allowed_tools: tuple[str, ...] = ()
    required_tools: tuple[str, ...] = ()
    instruction_resources: tuple[str, ...] = ()
    assets: tuple[str, ...] = ()
    tests: tuple[str, ...] = ()
    dependencies: SkillDependencies = Field(default_factory=SkillDependencies)
    scripts: tuple[SkillScriptDefinition, ...] = ()


class SkillDescriptor(SkillModel):
    """发现阶段可见的轻量 Skill 元数据，不包含正文。"""

    name: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$", max_length=64)
    description: str = Field(min_length=1, max_length=1024)
    version: str
    skill_root: Path
    skill_file: Path
    manifest_file: Path | None = None
    package_format: Literal["legacy", "v2"] = "legacy"
    metadata_hash: str = Field(min_length=64, max_length=64)
    allowed_tools: tuple[str, ...] = ()
    required_tools: tuple[str, ...] = ()
    modes: tuple[Literal["diagnose", "fix"], ...] = ("diagnose", "fix")
    tags: tuple[str, ...] = ()
    triggers: tuple[str, ...] = ()
    declared_resources: tuple[str, ...] = ()
    instruction_resources: tuple[str, ...] = ()
    assets: tuple[str, ...] = ()
    tests: tuple[str, ...] = ()
    dependencies: SkillDependencies = Field(default_factory=SkillDependencies)
    scripts: tuple[SkillScriptDefinition, ...] = ()


class SkillDiagnostic(SkillModel):
    """发现阶段对单个目录的可观察诊断。"""

    path: Path
    level: Literal["warning", "error"]
    code: str
    message: str


class SkillDiscoveryResult(SkillModel):
    """一次目录扫描的有效 Skill 与被跳过原因。"""

    skills: tuple[SkillDescriptor, ...]
    diagnostics: tuple[SkillDiagnostic, ...]


class SkillRouteMatch(SkillModel):
    """可解释的确定性 Skill 匹配结果。"""

    skill: SkillDescriptor
    score: int = Field(ge=1)
    reasons: tuple[str, ...]


class SkillSnapshot(SkillModel):
    """写入运行状态或 Checkpoint 的 Skill 身份快照。"""

    name: str
    version: str
    content_hash: str = Field(min_length=64, max_length=64)


class SkillResource(SkillModel):
    """按需读取的文本资源及其内容指纹。"""

    skill_name: str
    relative_path: str
    content: str
    content_hash: str = Field(min_length=64, max_length=64)


class ActivatedSkill(SkillModel):
    """完成正文加载和权限求交后的 Skill。"""

    descriptor: SkillDescriptor
    instructions: str = Field(min_length=1)
    content_hash: str = Field(min_length=64, max_length=64)
    effective_tools: tuple[str, ...]
    references: tuple[str, ...] = ()
    loaded_resources: tuple[SkillResource, ...] = ()
    scripts: tuple[SkillScriptDefinition, ...] = ()

    @property
    def snapshot(self) -> SkillSnapshot:
        """返回可持久化、可在恢复时校验的最小身份。"""

        return SkillSnapshot(
            name=self.descriptor.name,
            version=self.descriptor.version,
            content_hash=self.content_hash,
        )

    def render_instructions(self) -> str:
        """把版本、正文和激活资源组成可信指令。"""

        sections = [
            f"Skill: {self.descriptor.name}\n"
            f"版本: {self.descriptor.version}\n"
            f"内容哈希: {self.content_hash}\n\n"
            f"{self.instructions}"
        ]
        if self.loaded_resources:
            sections.append("\n\n# 已加载参考资料")
            sections.extend(
                f"\n\n## {resource.relative_path}\n\n{resource.content}"
                for resource in self.loaded_resources
            )
        if self.scripts:
            sections.append("\n\n# 确定性脚本工具")
            sections.extend(
                f"\n- {script.tool_name}: {script.description}"
                for script in self.scripts
            )
        return "".join(sections)


class SkillScriptContract(SkillModel):
    """已经读取并校验 Schema 的脚本运行契约。"""

    skill_name: str
    skill_root: Path
    definition: SkillScriptDefinition
    script_path: Path
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    contract_hash: str = Field(min_length=64, max_length=64)
