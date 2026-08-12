"""隔离候选修改和客观评估能力。"""

from .evaluator import ObjectiveCandidateEvaluator
from .models import (
    AppliedFileChange,
    CandidateEvaluationConfig,
    CandidateEvaluationReport,
    CandidateFileChange,
    CandidatePatch,
    CandidatePromotionResult,
    PatchApplicationResult,
    ValidationCheck,
)
from .generation import (
    CandidateFileChangeDraft,
    CandidatePatchDraft,
    PatchGenerationError,
    PatchTargetSelection,
    StructuredCandidatePatchGenerator,
)
from .patching import (
    CandidatePatchApplier,
    CandidatePatchConflictError,
    CandidatePatchError,
    CandidatePatchPermissionError,
)
from .workspace import (
    CandidateWorkspace,
    CandidateWorkspaceClosedError,
    CandidateWorkspaceConfig,
    CandidateWorkspaceError,
    CandidateWorkspaceLimitError,
    sha256_bytes,
)
from .promotion import (
    CandidatePatchPromoter,
    CandidatePromotionConflictError,
    CandidatePromotionError,
)

__all__ = [
    "AppliedFileChange",
    "CandidateEvaluationConfig",
    "CandidateEvaluationReport",
    "CandidateFileChange",
    "CandidatePatch",
    "CandidatePatchDraft",
    "CandidatePatchApplier",
    "CandidatePatchConflictError",
    "CandidatePatchError",
    "CandidatePatchPermissionError",
    "CandidatePatchPromoter",
    "CandidatePromotionConflictError",
    "CandidatePromotionError",
    "CandidatePromotionResult",
    "CandidateFileChangeDraft",
    "CandidateWorkspace",
    "CandidateWorkspaceClosedError",
    "CandidateWorkspaceConfig",
    "CandidateWorkspaceError",
    "CandidateWorkspaceLimitError",
    "ObjectiveCandidateEvaluator",
    "PatchGenerationError",
    "PatchTargetSelection",
    "PatchApplicationResult",
    "ValidationCheck",
    "sha256_bytes",
    "StructuredCandidatePatchGenerator",
]
