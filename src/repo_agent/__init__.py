"""RepoAgent 核心包。"""

from .application import (
    RepoAgentApplication,
    RepoAgentApplicationConfig,
    RepoAgentApplicationResult,
)

from .candidate import (
    CandidatePatch,
    CandidatePatchApplier,
    CandidateWorkspace,
    ObjectiveCandidateEvaluator,
)
from .maintenance import MaintenanceProposal, RepoAgentMaintenanceService
from .projects import (
    AmbiguousProjectSelectionError,
    DuplicateProjectNameError,
    DuplicateProjectPathError,
    InvalidProjectNameError,
    InvalidRepositoryError,
    PathOutsideRepositoryError,
    ProjectContext,
    ProjectContextResolver,
    ProjectNotFoundError,
    ProjectRegistration,
    ProjectRegistry,
    ProjectSelectionRequiredError,
    RegistryCorruptedError,
    RepositoryInspection,
    inspect_repository,
)
from .react import ReActConfig, ReActExecutor
from .tools import (
    LocalRepositoryTools,
    RepositoryToolPort,
    ToolRegistry,
    build_repository_tool_registry,
)
from .workflow import RepoAgentWorkflow, SQLiteWorkflowRuntime, WorkflowConfig

__all__ = [
    "AmbiguousProjectSelectionError",
    "CandidatePatch",
    "CandidatePatchApplier",
    "CandidateWorkspace",
    "DuplicateProjectNameError",
    "DuplicateProjectPathError",
    "InvalidProjectNameError",
    "InvalidRepositoryError",
    "LocalRepositoryTools",
    "MaintenanceProposal",
    "ObjectiveCandidateEvaluator",
    "PathOutsideRepositoryError",
    "ProjectContext",
    "ProjectContextResolver",
    "ProjectNotFoundError",
    "ProjectRegistration",
    "ProjectRegistry",
    "ProjectSelectionRequiredError",
    "RepoAgentApplication",
    "RepoAgentApplicationConfig",
    "RepoAgentApplicationResult",
    "RepoAgentMaintenanceService",
    "ReActConfig",
    "ReActExecutor",
    "RegistryCorruptedError",
    "RepositoryInspection",
    "RepositoryToolPort",
    "RepoAgentWorkflow",
    "SQLiteWorkflowRuntime",
    "ToolRegistry",
    "WorkflowConfig",
    "build_repository_tool_registry",
    "inspect_repository",
]
