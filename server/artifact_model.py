"""§23 Artifact Model — execution workspace and published artifact are distinct.

Workspace = mutable execution state
Artifact = explicit output

Temporary build/render files MUST NOT automatically become published artifacts.
"""

from __future__ import annotations

import hashlib
import time
import uuid
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any


class ArtifactType(StrEnum):
    BUILD_OUTPUT = "BUILD_OUTPUT"
    RENDER = "RENDER"
    REPORT = "REPORT"
    DIFF = "DIFF"
    LOG = "LOG"
    BINARY = "BINARY"
    DOCUMENTATION = "DOCUMENTATION"
    CUSTOM = "CUSTOM"


@dataclass(frozen=True)
class Artifact:
    """A published artifact — an explicit output of execution."""

    artifact_id: str
    goal_run_id: str
    execution_id: str
    type: ArtifactType
    locator: str
    digest: str = ""
    provenance: str = ""
    created_at: float = field(default_factory=time.time)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["type"] = self.type.value
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Artifact:
        data = dict(data)
        if isinstance(data.get("type"), str):
            data["type"] = ArtifactType(data["type"])
        return cls(**data)


def new_artifact(
    *,
    goal_run_id: str,
    execution_id: str,
    artifact_type: ArtifactType,
    locator: str,
    content: bytes | None = None,
    provenance: str = "",
) -> Artifact:
    """Create a new published artifact."""
    digest = hashlib.sha256(content).hexdigest() if content else ""
    return Artifact(
        artifact_id=str(uuid.uuid4()),
        goal_run_id=goal_run_id,
        execution_id=execution_id,
        type=artifact_type,
        locator=locator,
        digest=digest,
        provenance=provenance,
    )


def is_temporary_path(path: str) -> bool:
    """Check if a path is a temporary build/render file (not a published artifact)."""
    temp_indicators = [
        "/tmp/", "/var/tmp/", ".tmp", ".temp", ".cache/",
        "node_modules/.cache/", "__pycache__/", ".build/",
        "dist/", "target/", ".veya/runs/", ".veya/worktrees/",
    ]
    return any(indicator in path for indicator in temp_indicators)
