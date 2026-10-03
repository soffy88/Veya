"""P0-05 Workspace Authority contracts — Revision, Projection, MutationSet, Lease.

These are the canonical contracts for workspace authority semantics.
They do NOT replace the existing worktree implementation; they formalize
the authority/projection/reconciliation layer on top of it.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any


class ConflictClass(StrEnum):
    PATH_CONFLICT = "PATH_CONFLICT"
    REVISION_CONFLICT = "REVISION_CONFLICT"
    GIT_DIVERGENCE = "GIT_DIVERGENCE"
    LEASE_CONFLICT = "LEASE_CONFLICT"
    AUTHORITY_CHANGED = "AUTHORITY_CHANGED"


class ConflictStrategy(StrEnum):
    REBASE = "REBASE"
    MERGE = "MERGE"
    RETRY = "RETRY"
    REPROJECT = "REPROJECT"
    REJECT = "REJECT"
    HUMAN_REVIEW = "HUMAN_REVIEW"


def classify_conflict(
    *,
    base_mismatch: bool = False,
    path_overlap: bool = False,
    git_diverged: bool = False,
    lease_held: bool = False,
    authority_changed: bool = False,
) -> ConflictClass | None:
    """Classify a workspace conflict. Returns None if no conflict."""
    if authority_changed:
        return ConflictClass.AUTHORITY_CHANGED
    if lease_held:
        return ConflictClass.LEASE_CONFLICT
    if git_diverged:
        return ConflictClass.GIT_DIVERGENCE
    if base_mismatch:
        return ConflictClass.REVISION_CONFLICT
    if path_overlap:
        return ConflictClass.PATH_CONFLICT
    return None


def resolve_conflict(
    conflict: ConflictClass,
    *,
    allow_human_review: bool = True,
) -> ConflictStrategy:
    """Determine the resolution strategy for a conflict class."""
    if conflict is ConflictClass.AUTHORITY_CHANGED:
        return ConflictStrategy.HUMAN_REVIEW
    if conflict is ConflictClass.LEASE_CONFLICT:
        return ConflictStrategy.RETRY
    if conflict is ConflictClass.GIT_DIVERGENCE:
        return ConflictStrategy.REBASE
    if conflict is ConflictClass.REVISION_CONFLICT:
        return ConflictStrategy.MERGE
    if conflict is ConflictClass.PATH_CONFLICT:
        return ConflictStrategy.REPROJECT
    if allow_human_review:
        return ConflictStrategy.HUMAN_REVIEW
    return ConflictStrategy.REJECT


class AuthorityType(StrEnum):
    GIT = "GIT"
    FILESYSTEM = "FILESYSTEM"
    HYBRID = "HYBRID"


class ProjectionState(StrEnum):
    CREATING = "CREATING"
    READY = "READY"
    ACTIVE = "ACTIVE"
    STALE = "STALE"
    RECONCILING = "RECONCILING"
    CONFLICT = "CONFLICT"
    INVALID = "INVALID"
    DESTROYED = "DESTROYED"


@dataclass(frozen=True)
class WorkspaceRevision:
    """Canonical workspace revision identity."""

    revision_id: str
    workspace_id: str
    commit_sha: str | None = None
    tree_sha: str | None = None
    monotonic: int = 0
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class WorkspaceAuthority:
    """Single canonical authority per workspace."""

    workspace_id: str
    canonical_root: str
    authority_type: AuthorityType
    current_revision: WorkspaceRevision
    repositories: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "workspace_id": self.workspace_id,
            "canonical_root": self.canonical_root,
            "authority_type": self.authority_type.value,
            "current_revision": self.current_revision.to_dict(),
            "repositories": list(self.repositories),
        }


@dataclass(frozen=True)
class WorkspaceProjection:
    """A projection of the workspace authority for execution."""

    projection_id: str
    workspace_id: str
    base_revision: WorkspaceRevision
    generation: int = 0
    backend: str = "local"
    path: str = ""
    state: ProjectionState = ProjectionState.CREATING
    lease_id: str | None = None
    created_at: float = field(default_factory=time.time)
    last_reconciled_at: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "projection_id": self.projection_id,
            "workspace_id": self.workspace_id,
            "base_revision": self.base_revision.to_dict(),
            "generation": self.generation,
            "backend": self.backend,
            "path": self.path,
            "state": self.state.value,
            "lease_id": self.lease_id,
            "created_at": self.created_at,
            "last_reconciled_at": self.last_reconciled_at,
        }


@dataclass(frozen=True)
class WorkspaceMutationSet:
    """The set of mutations produced by a projected execution."""

    mutation_id: str
    workspace_id: str
    projection_id: str
    base_revision: WorkspaceRevision
    changed_paths: tuple[str, ...] = ()
    deleted_paths: tuple[str, ...] = ()
    git_state: dict[str, Any] | None = None
    generated_artifacts: tuple[str, ...] = ()
    execution_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "mutation_id": self.mutation_id,
            "workspace_id": self.workspace_id,
            "projection_id": self.projection_id,
            "base_revision": self.base_revision.to_dict(),
            "changed_paths": list(self.changed_paths),
            "deleted_paths": list(self.deleted_paths),
            "git_state": self.git_state,
            "generated_artifacts": list(self.generated_artifacts),
            "execution_id": self.execution_id,
        }


@dataclass(frozen=True)
class ExecutionLease:
    """Lease preventing unsafe simultaneous promotion."""

    lease_id: str
    workspace_id: str
    projection_id: str
    owner_execution: str
    scope: str = "write"
    expiry: float = field(default_factory=lambda: time.time() + 3600)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def new_revision(workspace_id: str, *, commit_sha: str | None = None, monotonic: int = 0) -> WorkspaceRevision:
    return WorkspaceRevision(
        revision_id=str(uuid.uuid4()),
        workspace_id=workspace_id,
        commit_sha=commit_sha,
        monotonic=monotonic,
    )


def new_projection(
    workspace_id: str,
    base_revision: WorkspaceRevision,
    *,
    backend: str = "local",
    path: str = "",
) -> WorkspaceProjection:
    return WorkspaceProjection(
        projection_id=str(uuid.uuid4()),
        workspace_id=workspace_id,
        base_revision=base_revision,
        backend=backend,
        path=path,
        state=ProjectionState.READY,
    )


def new_mutation_set(
    workspace_id: str,
    projection_id: str,
    base_revision: WorkspaceRevision,
    *,
    execution_id: str = "",
) -> WorkspaceMutationSet:
    return WorkspaceMutationSet(
        mutation_id=str(uuid.uuid4()),
        workspace_id=workspace_id,
        projection_id=projection_id,
        base_revision=base_revision,
        execution_id=execution_id,
    )


def new_lease(
    workspace_id: str,
    projection_id: str,
    owner_execution: str,
    *,
    scope: str = "write",
    ttl_seconds: float = 3600,
) -> ExecutionLease:
    return ExecutionLease(
        lease_id=str(uuid.uuid4()),
        workspace_id=workspace_id,
        projection_id=projection_id,
        owner_execution=owner_execution,
        scope=scope,
        expiry=time.time() + ttl_seconds,
    )


def is_stale(projection: WorkspaceProjection, authority: WorkspaceAuthority) -> bool:
    """A projection is stale if its base revision doesn't match the authority."""
    return projection.base_revision.revision_id != authority.current_revision.revision_id


def reconcile_projection(
    projection: WorkspaceProjection,
    authority: WorkspaceAuthority,
) -> tuple[ProjectionState, str]:
    """Reconcile a projection against the authority.

    Returns (state, reason). Never silently overwrites.
    """
    if projection.base_revision.revision_id == authority.current_revision.revision_id:
        return ProjectionState.READY, "MATCH"
    if projection.state is ProjectionState.CONFLICT:
        return ProjectionState.CONFLICT, "already in conflict"
    return ProjectionState.STALE, "DIVERGED: base revision mismatch"
