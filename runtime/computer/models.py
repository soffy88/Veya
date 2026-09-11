"""P1-A Persistent Computer: Metadata models for persistent computer state."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Any, Literal

ComputerLifecycleState = Literal[
    "created",
    "running",
    "stopped",
    "checkpointing",
    "restoring",
    "terminated",
    "failed",
]


class CredentialType(Enum):
    """Types of credentials stored in the computer."""
    API_KEY = "api_key"
    OAUTH_TOKEN = "oauth_token"
    SSH_KEY = "ssh_key"
    CERTIFICATE = "certificate"
    PASSWORD = "password"
    CUSTOM = "custom"


@dataclass(frozen=True)
class CredentialRef:
    """Reference to a credential (never stores plaintext)."""
    ref_id: str
    type: CredentialType
    name: str
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    def to_dict(self) -> dict[str, Any]:
        return {
            "ref_id": self.ref_id,
            "type": self.type.value,
            "name": self.name,
            "metadata": self.metadata,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CredentialRef:
        return cls(
            ref_id=data["ref_id"],
            type=CredentialType(data["type"]),
            name=data["name"],
            metadata=data.get("metadata", {}),
            created_at=data.get("created_at", datetime.now(UTC).isoformat()),
        )


@dataclass(frozen=True)
class CheckpointRef:
    """Reference to a checkpoint."""
    checkpoint_id: str
    computer_id: str
    goal_run_id: str | None
    path: str
    sha256: str
    size_bytes: int
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CheckpointRef:
        return cls(**data)


@dataclass(frozen=True)
class PersistentComputer:
    """Persistent computer metadata.

    Fields:
    - computer_id: Stable ID across supervisor restarts
    - owner/principal: Who owns this computer
    - workspace_ref: Reference to workspace filesystem
    - browser_profile_ref: Reference to browser profile
    - downloads_ref: Reference to downloads directory
    - credential_refs: List of credential references (no plaintext)
    - goal_run_refs: List of GoalRun IDs correlated with this computer
    - checkpoint_ref: Latest checkpoint reference
    - lifecycle_state: Current state
    - created_at: Creation timestamp
    - last_active_at: Last activity timestamp
    - version: Schema version
    """
    computer_id: str
    owner_id: str
    workspace_ref: str
    browser_profile_ref: str | None = None
    downloads_ref: str | None = None
    credential_refs: list[CredentialRef] = field(default_factory=list)
    goal_run_refs: list[str] = field(default_factory=list)
    checkpoint_ref: CheckpointRef | None = None
    lifecycle_state: ComputerLifecycleState = "created"
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    last_active_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    version: str = "1.0"

    def to_dict(self) -> dict[str, Any]:
        return {
            "computer_id": self.computer_id,
            "owner_id": self.owner_id,
            "workspace_ref": self.workspace_ref,
            "browser_profile_ref": self.browser_profile_ref,
            "downloads_ref": self.downloads_ref,
            "credential_refs": [c.to_dict() for c in self.credential_refs],
            "goal_run_refs": self.goal_run_refs,
            "checkpoint_ref": self.checkpoint_ref.to_dict() if self.checkpoint_ref else None,
            "lifecycle_state": self.lifecycle_state,
            "created_at": self.created_at,
            "last_active_at": self.last_active_at,
            "version": self.version,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PersistentComputer:
        return cls(
            computer_id=data["computer_id"],
            owner_id=data["owner_id"],
            workspace_ref=data["workspace_ref"],
            browser_profile_ref=data.get("browser_profile_ref"),
            downloads_ref=data.get("downloads_ref"),
            credential_refs=[CredentialRef.from_dict(c) for c in data.get("credential_refs", [])],
            goal_run_refs=data.get("goal_run_refs", []),
            checkpoint_ref=CheckpointRef.from_dict(data["checkpoint_ref"]) if data.get("checkpoint_ref") else None,
            lifecycle_state=data.get("lifecycle_state", "created"),
            created_at=data.get("created_at", datetime.now(UTC).isoformat()),
            last_active_at=data.get("last_active_at", datetime.now(UTC).isoformat()),
            version=data.get("version", "1.0"),
        )

    def compute_hash(self) -> str:
        """Compute deterministic hash of the computer state (excluding timestamps)."""
        data = {
            k: v for k, v in self.to_dict().items()
            if k not in ("created_at", "last_active_at", "checkpoint_ref")
        }
        if data.get("checkpoint_ref"):
            data["checkpoint_ref"] = {k: v for k, v in data["checkpoint_ref"].items() if k != "created_at"}
        return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()[:32]

    def add_goal_run(self, goal_run_id: str) -> PersistentComputer:
        """Return new computer with added goal run reference."""
        if goal_run_id not in self.goal_run_refs:
            new_refs = [*list(self.goal_run_refs), goal_run_id]
            return PersistentComputer(
                computer_id=self.computer_id,
                owner_id=self.owner_id,
                workspace_ref=self.workspace_ref,
                browser_profile_ref=self.browser_profile_ref,
                downloads_ref=self.downloads_ref,
                credential_refs=self.credential_refs,
                goal_run_refs=new_refs,
                checkpoint_ref=self.checkpoint_ref,
                lifecycle_state=self.lifecycle_state,
                created_at=self.created_at,
                last_active_at=datetime.now(UTC).isoformat(),
                version=self.version,
            )
        return self

    def remove_goal_run(self, goal_run_id: str) -> PersistentComputer:
        """Return new computer with removed goal run reference."""
        new_refs = [g for g in self.goal_run_refs if g != goal_run_id]
        return PersistentComputer(
            computer_id=self.computer_id,
            owner_id=self.owner_id,
            workspace_ref=self.workspace_ref,
            browser_profile_ref=self.browser_profile_ref,
            downloads_ref=self.downloads_ref,
            credential_refs=self.credential_refs,
            goal_run_refs=new_refs,
            checkpoint_ref=self.checkpoint_ref,
            lifecycle_state=self.lifecycle_state,
            created_at=self.created_at,
            last_active_at=datetime.now(UTC).isoformat(),
            version=self.version,
        )

    def with_checkpoint(self, checkpoint: CheckpointRef) -> PersistentComputer:
        """Return new computer with updated checkpoint."""
        return PersistentComputer(
            computer_id=self.computer_id,
            owner_id=self.owner_id,
            workspace_ref=self.workspace_ref,
            browser_profile_ref=self.browser_profile_ref,
            downloads_ref=self.downloads_ref,
            credential_refs=self.credential_refs,
            goal_run_refs=self.goal_run_refs,
            checkpoint_ref=checkpoint,
            lifecycle_state=self.lifecycle_state,
            created_at=self.created_at,
            last_active_at=datetime.now(UTC).isoformat(),
            version=self.version,
        )

    def with_state(self, state: ComputerLifecycleState) -> PersistentComputer:
        """Return new computer with updated lifecycle state."""
        return PersistentComputer(
            computer_id=self.computer_id,
            owner_id=self.owner_id,
            workspace_ref=self.workspace_ref,
            browser_profile_ref=self.browser_profile_ref,
            downloads_ref=self.downloads_ref,
            credential_refs=self.credential_refs,
            goal_run_refs=self.goal_run_refs,
            checkpoint_ref=self.checkpoint_ref,
            lifecycle_state=state,
            created_at=self.created_at,
            last_active_at=datetime.now(UTC).isoformat(),
            version=self.version,
        )

    def with_browser_profile(self, browser_profile_ref: str) -> PersistentComputer:
        """Return new computer with browser profile reference."""
        return PersistentComputer(
            computer_id=self.computer_id,
            owner_id=self.owner_id,
            workspace_ref=self.workspace_ref,
            browser_profile_ref=browser_profile_ref,
            downloads_ref=self.downloads_ref,
            credential_refs=self.credential_refs,
            goal_run_refs=self.goal_run_refs,
            checkpoint_ref=self.checkpoint_ref,
            lifecycle_state=self.lifecycle_state,
            created_at=self.created_at,
            last_active_at=datetime.now(UTC).isoformat(),
            version=self.version,
        )

    def with_downloads(self, downloads_ref: str) -> PersistentComputer:
        """Return new computer with downloads reference."""
        return PersistentComputer(
            computer_id=self.computer_id,
            owner_id=self.owner_id,
            workspace_ref=self.workspace_ref,
            browser_profile_ref=self.browser_profile_ref,
            downloads_ref=downloads_ref,
            credential_refs=self.credential_refs,
            goal_run_refs=self.goal_run_refs,
            checkpoint_ref=self.checkpoint_ref,
            lifecycle_state=self.lifecycle_state,
            created_at=self.created_at,
            last_active_at=datetime.now(UTC).isoformat(),
            version=self.version,
        )

    def add_credential(self, credential: CredentialRef) -> PersistentComputer:
        """Return new computer with added credential."""
        if not any(c.ref_id == credential.ref_id for c in self.credential_refs):
            new_creds = [*list(self.credential_refs), credential]
            return PersistentComputer(
                computer_id=self.computer_id,
                owner_id=self.owner_id,
                workspace_ref=self.workspace_ref,
                browser_profile_ref=self.browser_profile_ref,
                downloads_ref=self.downloads_ref,
                credential_refs=new_creds,
                goal_run_refs=self.goal_run_refs,
                checkpoint_ref=self.checkpoint_ref,
                lifecycle_state=self.lifecycle_state,
                created_at=self.created_at,
                last_active_at=datetime.now(UTC).isoformat(),
                version=self.version,
            )
        return self

    def remove_credential(self, ref_id: str) -> PersistentComputer:
        """Return new computer with removed credential."""
        new_creds = [c for c in self.credential_refs if c.ref_id != ref_id]
        return PersistentComputer(
            computer_id=self.computer_id,
            owner_id=self.owner_id,
            workspace_ref=self.workspace_ref,
            browser_profile_ref=self.browser_profile_ref,
            downloads_ref=self.downloads_ref,
            credential_refs=new_creds,
            goal_run_refs=self.goal_run_refs,
            checkpoint_ref=self.checkpoint_ref,
            lifecycle_state=self.lifecycle_state,
            created_at=self.created_at,
            last_active_at=datetime.now(UTC).isoformat(),
            version=self.version,
        )


@dataclass(frozen=True)
class ComputerSession:
    """Active session metadata for a running computer."""
    session_id: str
    computer_id: str
    owner_id: str
    supervisor_id: str
    started_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    state: Literal["active", "paused", "terminating"] = "active"
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ComputerSession:
        return cls(**data)


def generate_computer_id(owner_id: str, workspace: str) -> str:
    """Generate a stable computer ID from owner and workspace.

    This ensures the same owner+workspace always produces the same computer_id
    across supervisor restarts.
    """
    data = f"{owner_id}:{workspace}"
    return "comp-" + hashlib.sha256(data.encode()).hexdigest()[:24]


__all__ = [
    "CheckpointRef",
    "ComputerLifecycleState",
    "ComputerSession",
    "CredentialRef",
    "CredentialType",
    "PersistentComputer",
    "generate_computer_id",
]
