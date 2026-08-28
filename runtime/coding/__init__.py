"""Local coding-product capabilities below Veya's single MasterAgent path.

The package owns workspace discovery, isolated worktrees, sandbox policy, and
coding evidence.  It deliberately does not own orchestration or durable
execution; those remain the responsibility of the existing MasterAgent and
Execution Runtime layers.
"""

from .models import (
    CodingRun,
    CodingTask,
    CodingWorkspace,
    CommandResult,
    PatchArtifact,
    VerificationReport,
)
from .workspace_detect import detect_workspace, infer_commands

__all__ = [
    "CodingRun",
    "CodingTask",
    "CodingWorkspace",
    "CommandResult",
    "PatchArtifact",
    "VerificationReport",
    "detect_workspace",
    "infer_commands",
]
