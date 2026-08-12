"""RepoAgent 的可版本化、渐进加载 Skill 系统。"""

from .catalog import (
    SkillActivationError,
    SkillCatalog,
    SkillCatalogError,
    SkillChangedError,
    SkillNotFoundError,
    SkillResourceError,
)
from .models import (
    ActivatedSkill,
    SkillDescriptor,
    SkillDiagnostic,
    SkillDiscoveryResult,
    SkillDependencies,
    SkillPackageManifest,
    SkillResource,
    SkillRouteMatch,
    SkillScriptContract,
    SkillScriptDefinition,
    SkillSnapshot,
)
from .routing import SkillRouter
from .runtime import (
    SkillAwareReActExecutor,
    SkillAwareRunResult,
    SkillManager,
)
from .scripts import (
    SkillScriptExecutor,
    register_skill_script_tools,
    skill_script_scope,
)

__all__ = [
    "ActivatedSkill",
    "SkillActivationError",
    "SkillAwareReActExecutor",
    "SkillAwareRunResult",
    "SkillCatalog",
    "SkillCatalogError",
    "SkillChangedError",
    "SkillDescriptor",
    "SkillDependencies",
    "SkillDiagnostic",
    "SkillDiscoveryResult",
    "SkillManager",
    "SkillNotFoundError",
    "SkillPackageManifest",
    "SkillResource",
    "SkillResourceError",
    "SkillRouteMatch",
    "SkillRouter",
    "SkillScriptContract",
    "SkillScriptDefinition",
    "SkillScriptExecutor",
    "SkillSnapshot",
    "register_skill_script_tools",
    "skill_script_scope",
]
