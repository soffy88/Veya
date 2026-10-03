"""§27 Versioning — version-addressable specs for reproducibility/evaluation.

GoalRun SHOULD record relevant versions for reproducibility/evaluation.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class VersionRecord:
    """Version attribution for a GoalRun."""

    goal_run_id: str
    agent_spec_version: str = ""
    skill_versions: dict[str, str] = field(default_factory=dict)
    workflow_versions: dict[str, str] = field(default_factory=dict)
    policy_version: str = ""
    context_policy_version: str = ""
    evaluation_suite_version: str = ""
    harness_version: str = ""
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> VersionRecord:
        return cls(**data)


def new_version_record(
    goal_run_id: str,
    **kwargs: Any,
) -> VersionRecord:
    """Create a new version record for a GoalRun."""
    return VersionRecord(goal_run_id=goal_run_id, **kwargs)
