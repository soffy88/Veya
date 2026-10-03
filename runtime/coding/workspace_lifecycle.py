"""Generic workspace lifecycle (D4, Veya project runtime owner).

ONE authority for workspace identity, attachment, state, leases, recovery,
safe release/destroy decisions and audit projection. Underlying mechanisms
stay where they are — git worktree ops in ``WorktreeManager``, detection in
``workspace_detect``, sandboxes in ``sandbox_profiles`` — this module never
reimplements them and never moves them into 3O.

Frozen split (§7): file/workspace continuity != agent session continuity.
D3 decides session disposition; here we only decide whether a workspace is
reused / recovered / reset / replaced, joined by explicit refs.

P0-05/P1-07: the canonical authority/revision/projection contracts live in
``workspace_contracts`` and are recorded here as additive metadata on the same
lifecycle record — this module stays the single authority and never forks it.
A projection that diverged from the authority is surfaced as a classified
conflict instead of being silently re-pointed at a newer revision.

The module is stdlib-only besides sibling coding substrates. Audit emission
is injected (``emit``) so no ``server/`` dependency is introduced.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from runtime.coding.workspace_contracts import (
    AuthorityType,
    ConflictClass,
    ConflictStrategy,
    ExecutionLease,
    ProjectionState,
    WorkspaceAuthority,
    WorkspaceMutationSet,
    WorkspaceProjection,
    WorkspaceRevision,
    classify_conflict,
    is_stale,
    new_lease,
    new_mutation_set,
    new_projection,
    new_revision,
    reconcile_projection,
    resolve_conflict,
)
from runtime.coding.worktree import WorktreeError, WorktreeManager

__all__ = [
    "AUTHORITY_METADATA_KEY",
    "LEASES_METADATA_KEY",
    "MUTATION_METADATA_KEY",
    "PROJECTION_CONFLICT_KEY",
    "PROJECTION_METADATA_KEY",
    "WORKSPACE_EVENT_TOPICS",
    "WorkspaceBusyError",
    "WorkspaceConflictError",
    "WorkspaceError",
    "WorkspaceHandle",
    "WorkspaceKind",
    "WorkspacePathError",
    "WorkspaceSnapshot",
    "WorkspaceState",
    "WorkspaceStore",
    "WorkspaceUnsupportedError",
    "attach_workspace",
    "collect_garbage",
    "create_workspace",
    "destroy_workspace",
    "prepare_workspace",
    "recover_workspace",
    "release_workspace",
    "reset_workspace",
    "snapshot_workspace",
    "workspace_authority",
    "workspace_leases",
    "workspace_projection",
    "workspace_projection_conflict",
]

WORKSPACE_EVENT_TOPICS = (
    "workspace.created",
    "workspace.attached",
    "workspace.prepared",
    "workspace.snapshot",
    "workspace.recovering",
    "workspace.recovered",
    "workspace.reset",
    "workspace.released",
    "workspace.destroyed",
    "workspace.broken",
)


class WorkspaceError(WorktreeError):
    """Base error for workspace lifecycle decisions."""


class WorkspaceConflictError(WorkspaceError):
    """Same id requested with a different root/kind (deterministic conflict)."""


class WorkspaceBusyError(WorkspaceError):
    """Destructive/guarded operation refused while actively leased."""


class WorkspacePathError(WorkspaceError):
    """Path escapes the Veya-managed root (fail-closed)."""


class WorkspaceUnsupportedError(WorkspaceError):
    """No real backend implements this kind/operation (never faked)."""


class WorkspaceKind(StrEnum):
    """Workspace kinds. Only kinds with real implementations get behavior."""

    LOCAL = "local"
    GIT_WORKTREE = "git_worktree"
    SANDBOX = "sandbox"
    REMOTE = "remote"

    @classmethod
    def coerce(cls, value: WorkspaceKind | str) -> WorkspaceKind:
        if isinstance(value, cls):
            return value
        if isinstance(value, str):
            try:
                return cls(value.strip().lower())
            except ValueError:
                pass
        valid = sorted(member.value for member in cls)
        raise ValueError(f"unknown workspace kind {value!r}; expected one of {valid}")


class WorkspaceState(StrEnum):
    """Canonical lifecycle states (single authority)."""

    NEW = "new"
    PREPARING = "preparing"
    READY = "ready"
    IN_USE = "in_use"
    RELEASING = "releasing"
    RELEASED = "released"
    RECOVERING = "recovering"
    BROKEN = "broken"
    DESTROYED = "destroyed"


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True)
class WorkspaceHandle:
    """Generic workspace identity + lifecycle record (value object)."""

    workspace_id: str
    owner_scope: str
    root: str
    kind: WorkspaceKind = WorkspaceKind.LOCAL
    state: WorkspaceState = WorkspaceState.NEW
    created_at: str = field(default_factory=_utc_now)
    last_used_at: str = field(default_factory=_utc_now)
    generation: int = 0
    active_run_ids: tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.workspace_id.strip():
            raise ValueError("WorkspaceHandle.workspace_id must not be empty")
        if not self.root.strip():
            raise ValueError("WorkspaceHandle.root must not be empty")
        object.__setattr__(self, "kind", WorkspaceKind.coerce(self.kind))
        object.__setattr__(self, "state", _coerce_state(self.state))
        runs = sorted({str(item) for item in self.active_run_ids if str(item)})
        object.__setattr__(self, "active_run_ids", tuple(runs))
        if self.generation < 0:
            raise ValueError("WorkspaceHandle.generation must be >= 0")
        object.__setattr__(self, "metadata", dict(self.metadata))

    def to_dict(self) -> dict[str, Any]:
        return {
            "workspace_id": self.workspace_id,
            "owner_scope": self.owner_scope,
            "root": self.root,
            "kind": self.kind.value,
            "state": self.state.value,
            "created_at": self.created_at,
            "last_used_at": self.last_used_at,
            "generation": self.generation,
            "active_run_ids": list(self.active_run_ids),
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> WorkspaceHandle:
        raw = dict(data)
        runs = raw.get("active_run_ids") or ()
        raw["active_run_ids"] = tuple(runs)
        raw["metadata"] = dict(raw.get("metadata") or {})
        return cls(**raw)


def _coerce_state(value: WorkspaceState | str) -> WorkspaceState:
    if isinstance(value, WorkspaceState):
        return value
    if isinstance(value, str):
        try:
            return WorkspaceState(value.strip().lower())
        except ValueError:
            pass
    valid = sorted(member.value for member in WorkspaceState)
    raise ValueError(f"unknown workspace state {value!r}; expected one of {valid}")


@dataclass(frozen=True)
class WorkspaceSnapshot:
    """Recovery references only — never a filesystem copy.

    Distinct from ``ExecutionCheckpoint`` (scheduler state): this records
    what a workspace looked like so recovery can verify it, not execution
    progress.
    """

    workspace_id: str
    generation: int
    root: str
    branch: str | None = None
    clean: bool | None = None
    dirty_hash: str | None = None
    artifact_refs: tuple[str, ...] = ()
    timestamp: str = field(default_factory=_utc_now)

    def __post_init__(self) -> None:
        object.__setattr__(self, "artifact_refs", tuple(self.artifact_refs))

    def to_dict(self) -> dict[str, Any]:
        return {
            "workspace_id": self.workspace_id,
            "generation": self.generation,
            "root": self.root,
            "branch": self.branch,
            "clean": self.clean,
            "dirty_hash": self.dirty_hash,
            "artifact_refs": list(self.artifact_refs),
            "timestamp": self.timestamp,
        }


def _dirty_hash(changed_files: list[str]) -> str:
    digest = hashlib.sha256("\n".join(sorted(changed_files)).encode())
    return digest.hexdigest()[:16]


class WorkspaceStore:
    """Single workspace-state authority (records only, no registry fork).

    One JSON record per workspace id under ``<root>/workspaces/``; atomic
    writes like the sibling durable stores. There is exactly one store
    shape — no Coding/Generic/Bot variants.
    """

    def __init__(self, root: str | Path):
        self.root = Path(root).expanduser().resolve()
        self.base = self.root / "workspaces"

    def _path_for(self, workspace_id: str) -> Path:
        if not workspace_id or "/" in workspace_id or "\\" in workspace_id:
            raise ValueError(f"invalid workspace id: {workspace_id!r}")
        return self.base / f"{workspace_id}.json"

    def get(self, workspace_id: str) -> WorkspaceHandle | None:
        path = self._path_for(workspace_id)
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError:
            return None
        try:
            return WorkspaceHandle.from_dict(json.loads(raw))
        except (ValueError, TypeError, KeyError, AttributeError):
            return None

    def put(self, handle: WorkspaceHandle) -> WorkspaceHandle:
        self.base.mkdir(parents=True, exist_ok=True)
        path = self._path_for(handle.workspace_id)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(handle.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(path)
        return handle

    def list(self) -> list[WorkspaceHandle]:
        if not self.base.is_dir():
            return []
        handles: list[WorkspaceHandle] = []
        for path in sorted(self.base.glob("*.json")):
            handle = self.get(path.stem)
            if handle is not None:
                handles.append(handle)
        return handles


def _emit(
    emit: Callable[[str, dict[str, Any]], Any] | None,
    topic: str,
    handle: WorkspaceHandle,
    *,
    previous_state: str | None,
    task_id: str | None = None,
    run_id: str | None = None,
    reason: str = "",
) -> None:
    if emit is None:
        return
    emit(
        topic,
        {
            "workspace_id": handle.workspace_id,
            "task_id": task_id,
            "run_id": run_id,
            "generation": handle.generation,
            "previous_state": previous_state,
            "new_state": handle.state.value,
            "reason": reason,
        },
    )


def _live_runs(handle: WorkspaceHandle, is_live: Callable[[str], bool] | None) -> list[str]:
    """Active runs with liveness evidence.

    Without a checker nothing can be confirmed dead, so every recorded run
    counts as live (fail-closed). Stale record-only runs never block when a
    checker proves them dead.
    """
    if is_live is None:
        return list(handle.active_run_ids)
    return [run for run in handle.active_run_ids if is_live(run)]


def _touch(handle: WorkspaceHandle, **updates: Any) -> WorkspaceHandle:
    data = handle.to_dict()
    data.update(updates)
    data["last_used_at"] = _utc_now()
    return WorkspaceHandle.from_dict(data)


def _resolve_manager(kind: WorkspaceKind, handle: WorkspaceHandle, factory: Any | None) -> Any:
    if factory is not None:
        return factory() if callable(factory) else factory
    if kind is WorkspaceKind.GIT_WORKTREE:
        repo_root = handle.metadata.get("repo_root")
        if not repo_root:
            raise WorkspaceError("git_worktree handle needs metadata.repo_root")
        return WorktreeManager(repo_root)
    raise WorkspaceUnsupportedError(f"no worktree backend for kind {kind.value!r}")


# -- P0-05/P1-07 contract plane -------------------------------------------------
#
# Additive metadata on the same lifecycle record: the canonical authority/revision
# pair, the projection an execution was promoted against, the lease evidence and
# the mutation set. None of it decides a lifecycle state — the functions above
# remain the single authority. What it adds is that a projection whose base
# revision no longer matches the authority is classified and recorded as a
# conflict instead of being silently re-pointed at the newer revision.

AUTHORITY_METADATA_KEY = "authority"
PROJECTION_METADATA_KEY = "projection"
PROJECTION_CONFLICT_KEY = "projection_conflict"
LEASES_METADATA_KEY = "leases"
MUTATION_METADATA_KEY = "mutation_set"

_AUTHORITY_BY_KIND = {
    WorkspaceKind.GIT_WORKTREE: AuthorityType.GIT,
    WorkspaceKind.LOCAL: AuthorityType.FILESYSTEM,
    WorkspaceKind.SANDBOX: AuthorityType.HYBRID,
    WorkspaceKind.REMOTE: AuthorityType.HYBRID,
}


def _authority_type(handle: WorkspaceHandle) -> AuthorityType:
    return _AUTHORITY_BY_KIND.get(handle.kind, AuthorityType.HYBRID)


def _metadata_dict(handle: WorkspaceHandle, key: str) -> dict[str, Any]:
    value = handle.metadata.get(key)
    return dict(value) if isinstance(value, Mapping) else {}


def _revision_from(raw: Mapping[str, Any]) -> WorkspaceRevision:
    return WorkspaceRevision(
        revision_id=str(raw["revision_id"]),
        workspace_id=str(raw["workspace_id"]),
        commit_sha=raw.get("commit_sha"),
        tree_sha=raw.get("tree_sha"),
        monotonic=int(raw.get("monotonic") or 0),
        created_at=float(raw.get("created_at") or 0.0),
    )


def _authority_from(raw: Mapping[str, Any]) -> WorkspaceAuthority:
    return WorkspaceAuthority(
        workspace_id=str(raw["workspace_id"]),
        canonical_root=str(raw["canonical_root"]),
        authority_type=AuthorityType(raw["authority_type"]),
        current_revision=_revision_from(raw["current_revision"]),
        repositories=tuple(raw.get("repositories") or ()),
    )


def _projection_from(raw: Mapping[str, Any]) -> WorkspaceProjection:
    return WorkspaceProjection(
        projection_id=str(raw["projection_id"]),
        workspace_id=str(raw["workspace_id"]),
        base_revision=_revision_from(raw["base_revision"]),
        generation=int(raw.get("generation") or 0),
        backend=str(raw.get("backend") or "local"),
        path=str(raw.get("path") or ""),
        state=ProjectionState(raw["state"]),
        lease_id=raw.get("lease_id"),
        created_at=float(raw.get("created_at") or 0.0),
        last_reconciled_at=raw.get("last_reconciled_at"),
    )


def _mutation_set_from(raw: Mapping[str, Any]) -> WorkspaceMutationSet:
    return WorkspaceMutationSet(
        mutation_id=str(raw["mutation_id"]),
        workspace_id=str(raw["workspace_id"]),
        projection_id=str(raw["projection_id"]),
        base_revision=_revision_from(raw["base_revision"]),
        changed_paths=tuple(raw.get("changed_paths") or ()),
        deleted_paths=tuple(raw.get("deleted_paths") or ()),
        git_state=raw.get("git_state"),
        generated_artifacts=tuple(raw.get("generated_artifacts") or ()),
        execution_id=str(raw.get("execution_id") or ""),
    )


def _lease_from(raw: Mapping[str, Any]) -> ExecutionLease:
    return ExecutionLease(
        lease_id=str(raw["lease_id"]),
        workspace_id=str(raw["workspace_id"]),
        projection_id=str(raw["projection_id"]),
        owner_execution=str(raw["owner_execution"]),
        scope=str(raw.get("scope") or "write"),
        expiry=float(raw.get("expiry") or 0.0),
    )


def _with_metadata(handle: WorkspaceHandle, key: str, value: Any) -> WorkspaceHandle:
    metadata = dict(handle.metadata)
    metadata[key] = value
    return _touch(handle, metadata=metadata)


def _authority_of(handle: WorkspaceHandle) -> WorkspaceAuthority | None:
    raw = _metadata_dict(handle, AUTHORITY_METADATA_KEY)
    if not raw:
        return None
    try:
        return _authority_from(raw)
    except (KeyError, TypeError, ValueError):
        return None


def _projection_of(handle: WorkspaceHandle) -> WorkspaceProjection | None:
    raw = _metadata_dict(handle, PROJECTION_METADATA_KEY)
    if not raw:
        return None
    try:
        return _projection_from(raw)
    except (KeyError, TypeError, ValueError):
        return None


def workspace_authority(store: WorkspaceStore, workspace_id: str) -> WorkspaceAuthority | None:
    """Canonical authority recorded for a workspace (None when not recorded)."""
    handle = store.get(workspace_id)
    return None if handle is None else _authority_of(handle)


def workspace_projection(store: WorkspaceStore, workspace_id: str) -> WorkspaceProjection | None:
    """Projection recorded for a workspace (None when nothing was promoted)."""
    handle = store.get(workspace_id)
    return None if handle is None else _projection_of(handle)


def workspace_projection_conflict(
    store: WorkspaceStore, workspace_id: str
) -> dict[str, Any] | None:
    """Recorded reconciliation outcome for a diverged projection, if any."""
    handle = store.get(workspace_id)
    if handle is None:
        return None
    conflict = _metadata_dict(handle, PROJECTION_CONFLICT_KEY)
    return conflict or None


def _leases_of(handle: WorkspaceHandle) -> tuple[ExecutionLease, ...]:
    raw = _metadata_dict(handle, LEASES_METADATA_KEY)
    leases: list[ExecutionLease] = []
    for entry in raw.values():
        if not isinstance(entry, Mapping):
            continue
        try:
            leases.append(_lease_from(entry))
        except (KeyError, TypeError, ValueError):
            continue
    return tuple(leases)


def workspace_leases(store: WorkspaceStore, workspace_id: str) -> tuple[ExecutionLease, ...]:
    """Leases recorded for a workspace, in insertion order."""
    handle = store.get(workspace_id)
    return () if handle is None else _leases_of(handle)


def _recorded_changed_paths(handle: WorkspaceHandle) -> tuple[str, ...]:
    raw = _metadata_dict(handle, MUTATION_METADATA_KEY)
    if not raw:
        return ()
    try:
        return _mutation_set_from(raw).changed_paths
    except (KeyError, TypeError, ValueError):
        return ()


def _lease_for(handle: WorkspaceHandle, execution_id: str) -> ExecutionLease | None:
    if not execution_id:
        return None
    for lease in _leases_of(handle):
        if lease.owner_execution == execution_id:
            return lease
    return None


def _backend_of(handle: WorkspaceHandle) -> str:
    return handle.kind.value


def _execution_id(handle: WorkspaceHandle, task_id: str | None) -> str:
    """The run an execution binds to (evidence, not an occupancy decision)."""
    if task_id:
        return task_id
    return handle.active_run_ids[-1] if handle.active_run_ids else ""


def _git_diverged(handle: WorkspaceHandle, branch: str | None) -> bool:
    recorded_branch = handle.metadata.get("branch")
    if not recorded_branch or not branch:
        return False
    return str(recorded_branch) != branch


def _repositories(handle: WorkspaceHandle) -> tuple[str, ...]:
    repo_root = handle.metadata.get("repo_root")
    return (str(repo_root),) if repo_root else ()


def _record_authority(handle: WorkspaceHandle) -> WorkspaceHandle:
    """Record/refresh the canonical authority for the current lifecycle epoch.

    The revision is monotonic in the lifecycle generation: a generation bump
    (recovery / baseline reset) is a new canonical revision, everything else
    keeps the recorded identity. Idempotent.
    """
    existing = _authority_of(handle)
    if existing is not None and existing.current_revision.monotonic == handle.generation:
        return handle
    revision = new_revision(handle.workspace_id, monotonic=handle.generation)
    authority = WorkspaceAuthority(
        workspace_id=handle.workspace_id,
        canonical_root=handle.root,
        authority_type=_authority_type(handle),
        current_revision=revision,
        repositories=_repositories(handle),
    )
    return _with_metadata(handle, AUTHORITY_METADATA_KEY, authority.to_dict())


def _record_lease(handle: WorkspaceHandle, run_id: str) -> WorkspaceHandle:
    """Record a write lease for a bound run (evidence, not occupancy)."""
    projection = _projection_of(handle)
    lease = new_lease(
        handle.workspace_id,
        projection.projection_id if projection is not None else "",
        run_id,
        scope="write",
    )
    leases = _metadata_dict(handle, LEASES_METADATA_KEY)
    leases[run_id] = lease.to_dict()
    return _with_metadata(handle, LEASES_METADATA_KEY, leases)


def _drop_lease(handle: WorkspaceHandle, run_id: str) -> WorkspaceHandle:
    leases = _metadata_dict(handle, LEASES_METADATA_KEY)
    if run_id not in leases:
        return handle
    leases.pop(run_id)
    return _with_metadata(handle, LEASES_METADATA_KEY, leases)


def _drop_leases(handle: WorkspaceHandle) -> WorkspaceHandle:
    if not _metadata_dict(handle, LEASES_METADATA_KEY):
        return handle
    return _with_metadata(handle, LEASES_METADATA_KEY, {})


def _record_mutation_set(
    handle: WorkspaceHandle,
    *,
    execution_id: str,
    changed_paths: tuple[str, ...],
    git_state: Mapping[str, Any] | None,
) -> WorkspaceHandle:
    """Record the mutation set a projected execution produced."""
    projection = _projection_of(handle)
    if projection is None:
        return handle
    mutation_set = new_mutation_set(
        handle.workspace_id,
        projection.projection_id,
        projection.base_revision,
        execution_id=execution_id,
    )
    mutation_set = replace(
        mutation_set,
        changed_paths=changed_paths,
        git_state=dict(git_state) if git_state is not None else None,
    )
    return _with_metadata(handle, MUTATION_METADATA_KEY, mutation_set.to_dict())


def _classify_projection_divergence(
    handle: WorkspaceHandle,
    projection: WorkspaceProjection,
    authority: WorkspaceAuthority,
    *,
    git_diverged: bool = False,
    path_overlap: bool = False,
) -> tuple[ConflictClass | None, ConflictStrategy | None]:
    """Classify why a projection no longer matches its authority.

    Occupancy is deliberately not an input here: a live lease is already
    refused by the lifecycle guards (``WorkspaceBusyError``) before any
    promotion is attempted, and the recorded lease belongs to the very
    execution this projection is promoted for.
    """
    recorded_root = _metadata_dict(handle, AUTHORITY_METADATA_KEY).get("canonical_root")
    authority_changed = recorded_root is not None and str(recorded_root) != handle.root
    conflict = classify_conflict(
        base_mismatch=is_stale(projection, authority),
        path_overlap=path_overlap,
        git_diverged=git_diverged,
        authority_changed=authority_changed,
    )
    if conflict is None:
        return None, None
    return conflict, resolve_conflict(conflict)


def _record_conflict(
    handle: WorkspaceHandle,
    projection: WorkspaceProjection,
    authority: WorkspaceAuthority,
    conflict: ConflictClass,
    strategy: ConflictStrategy,
    reason: str,
) -> WorkspaceHandle:
    """Freeze the diverged projection as CONFLICT with its resolution strategy.

    The recorded base revision is deliberately left untouched: overwriting it
    would be the silent last-write-wins the contract forbids.
    """
    handle = _with_metadata(
        handle,
        PROJECTION_METADATA_KEY,
        replace(
            projection,
            state=ProjectionState.CONFLICT,
            last_reconciled_at=time.time(),
        ).to_dict(),
    )
    record = {
        "workspace_id": handle.workspace_id,
        "projection_id": projection.projection_id,
        "conflict": conflict.value,
        "strategy": strategy.value,
        "state": ProjectionState.CONFLICT.value,
        "reason": reason,
        "base_revision_id": projection.base_revision.revision_id,
        "authority_revision_id": authority.current_revision.revision_id,
        "detected_at": _utc_now(),
    }
    return _with_metadata(handle, PROJECTION_CONFLICT_KEY, record)


def _reconcile_recorded_projection(
    handle: WorkspaceHandle,
    *,
    git_diverged: bool = False,
    path_overlap: bool = False,
) -> WorkspaceHandle:
    """Surface a diverged projection as a conflict; never repair it silently."""
    authority = _authority_of(handle)
    projection = _projection_of(handle)
    if authority is None or projection is None:
        return handle
    state, reason = reconcile_projection(projection, authority)
    if state is ProjectionState.READY and not git_diverged and not path_overlap:
        return handle
    if git_diverged:
        reason = "DIVERGED: git head moved off the projected base"
    elif path_overlap:
        reason = "DIVERGED: changed paths overlap the projected base"
    conflict, strategy = _classify_projection_divergence(
        handle, projection, authority, git_diverged=git_diverged, path_overlap=path_overlap
    )
    if conflict is None or strategy is None:
        return handle
    return _record_conflict(handle, projection, authority, conflict, strategy, reason)


def _promote_projection(
    handle: WorkspaceHandle,
    *,
    execution_id: str,
    backend: str = "local",
    git_diverged: bool = False,
    path_overlap: bool = False,
) -> WorkspaceHandle:
    """Promote a projection for this execution, or record why it cannot be.

    An unresolved conflict is kept as-is: promotion never overwrites the base
    revision of a projection that diverged from the authority.
    """
    authority = _authority_of(handle)
    if authority is None:
        return handle
    projection = _projection_of(handle)
    if _metadata_dict(handle, PROJECTION_CONFLICT_KEY):
        return handle
    if projection is not None and (is_stale(projection, authority) or git_diverged or path_overlap):
        handle = _reconcile_recorded_projection(
            handle, git_diverged=git_diverged, path_overlap=path_overlap
        )
        if _metadata_dict(handle, PROJECTION_CONFLICT_KEY):
            return handle
    promoted = replace(
        new_projection(
            handle.workspace_id,
            authority.current_revision,
            backend=backend,
            path=handle.root,
        ),
        state=ProjectionState.ACTIVE,
    )
    lease = _lease_for(handle, execution_id)
    if lease is not None:
        promoted = replace(promoted, lease_id=lease.lease_id)
    return _with_metadata(handle, PROJECTION_METADATA_KEY, promoted.to_dict())


def create_workspace(
    store: WorkspaceStore,
    *,
    workspace_id: str,
    owner_scope: str,
    root: str,
    kind: WorkspaceKind | str = WorkspaceKind.LOCAL,
    metadata: Mapping[str, Any] | None = None,
    emit: Callable[[str, dict[str, Any]], Any] | None = None,
    task_id: str | None = None,
) -> WorkspaceHandle:
    """Establish workspace identity/record (not the underlying resources).

    Idempotent: same id with the same root/kind returns the stored record;
    same id with a different root/kind is a deterministic conflict.
    """
    resolved_kind = WorkspaceKind.coerce(kind)
    existing = store.get(workspace_id)
    if existing is not None:
        if existing.root != root or existing.kind is not resolved_kind:
            raise WorkspaceConflictError(
                f"workspace {workspace_id!r} already exists with a different root/kind"
            )
        recorded = _record_authority(existing)
        if recorded != existing:
            store.put(recorded)
        return recorded
    handle = WorkspaceHandle(
        workspace_id=workspace_id,
        owner_scope=owner_scope,
        root=root,
        kind=resolved_kind,
        state=WorkspaceState.NEW,
        metadata=dict(metadata or {}),
    )
    handle = _record_authority(handle)
    store.put(handle)
    _emit(
        emit,
        "workspace.created",
        handle,
        previous_state=None,
        task_id=task_id,
        reason="identity established",
    )
    return handle


def attach_workspace(
    store: WorkspaceStore,
    workspace_id: str,
    run_id: str,
    *,
    emit: Callable[[str, dict[str, Any]], Any] | None = None,
    task_id: str | None = None,
) -> WorkspaceHandle:
    """Bind a task/run to an existing workspace (idempotent per run)."""
    handle = store.get(workspace_id)
    if handle is None:
        raise WorkspaceError(f"unknown workspace: {workspace_id!r}")
    if handle.state in (WorkspaceState.BROKEN, WorkspaceState.DESTROYED):
        raise WorkspaceError(f"cannot attach {handle.state.value} workspace: {workspace_id!r}")
    if run_id in handle.active_run_ids:
        return handle
    previous = handle.state.value
    runs = tuple(sorted(set(handle.active_run_ids) | {run_id}))
    state = WorkspaceState.IN_USE
    handle = _touch(handle, active_run_ids=runs, state=state)
    handle = _record_lease(handle, run_id)
    store.put(handle)
    _emit(
        emit,
        "workspace.attached",
        handle,
        previous_state=previous,
        task_id=task_id,
        run_id=run_id,
        reason="run bound",
    )
    return handle


def prepare_workspace(
    store: WorkspaceStore,
    workspace_id: str,
    *,
    worktree_manager: Any | None = None,
    emit: Callable[[str, dict[str, Any]], Any] | None = None,
    task_id: str | None = None,
) -> WorkspaceHandle:
    """Verify the underlying environment is executable (no provisioning)."""
    handle = store.get(workspace_id)
    if handle is None:
        raise WorkspaceError(f"unknown workspace: {workspace_id!r}")
    if handle.state in (WorkspaceState.BROKEN, WorkspaceState.DESTROYED):
        raise WorkspaceError(f"cannot prepare {handle.state.value} workspace: {workspace_id!r}")
    branch: str | None = None
    changed_paths: tuple[str, ...] = ()
    if handle.kind is WorkspaceKind.GIT_WORKTREE:
        manager = _resolve_manager(handle.kind, handle, worktree_manager)
        record = manager.status(path=handle.root)  # raises when unregistered/missing
        branch = record.branch_name
        changed_paths = tuple(record.changed_files)
    elif handle.kind is WorkspaceKind.LOCAL:
        if not Path(handle.root).is_dir():
            raise WorkspaceError(f"workspace root is not a directory: {handle.root!r}")
    else:
        raise WorkspaceUnsupportedError(
            f"prepare has no real backend for kind {handle.kind.value!r}"
        )
    previous = handle.state.value
    if handle.state is not WorkspaceState.IN_USE:
        handle = _touch(handle, state=WorkspaceState.READY)
    else:
        handle = _touch(handle)
    # P0-05: the verified environment is what an execution is promoted against.
    # A projection that no longer matches the authority is recorded as a
    # conflict instead of being re-pointed at the newer revision.
    handle = _promote_projection(
        handle,
        execution_id=_execution_id(handle, task_id),
        backend=_backend_of(handle),
        git_diverged=_git_diverged(handle, branch),
        path_overlap=bool(set(changed_paths) & set(_recorded_changed_paths(handle))),
    )
    store.put(handle)
    _emit(
        emit,
        "workspace.prepared",
        handle,
        previous_state=previous,
        task_id=task_id,
        reason="environment verified",
    )
    return handle


def snapshot_workspace(
    store: WorkspaceStore,
    workspace_id: str,
    *,
    artifact_refs: list[str] | tuple[str, ...] = (),
    worktree_manager: Any | None = None,
    emit: Callable[[str, dict[str, Any]], Any] | None = None,
    task_id: str | None = None,
) -> WorkspaceSnapshot:
    """Record recovery references (never copies the filesystem)."""
    handle = store.get(workspace_id)
    if handle is None:
        raise WorkspaceError(f"unknown workspace: {workspace_id!r}")
    branch: str | None = None
    clean: bool | None = None
    dirty: str | None = None
    changed_paths: tuple[str, ...] = ()
    if handle.kind is WorkspaceKind.GIT_WORKTREE:
        manager = _resolve_manager(handle.kind, handle, worktree_manager)
        record = manager.status(path=handle.root)
        branch = record.branch_name
        clean = record.clean
        changed_paths = tuple(record.changed_files)
        dirty = _dirty_hash(record.changed_files)
    snapshot = WorkspaceSnapshot(
        workspace_id=handle.workspace_id,
        generation=handle.generation,
        root=handle.root,
        branch=branch,
        clean=clean,
        dirty_hash=dirty,
        artifact_refs=tuple(artifact_refs),
    )
    # P0-05: the mutation set the projected execution produced. Lifecycle state
    # is untouched; only the contract record is added.
    observed = _record_mutation_set(
        handle,
        execution_id=_execution_id(handle, task_id),
        changed_paths=changed_paths,
        git_state=None if branch is None else {"branch": branch, "clean": clean},
    )
    if observed != handle:
        store.put(observed)
        handle = observed
    _emit(
        emit,
        "workspace.snapshot",
        handle,
        previous_state=handle.state.value,
        task_id=task_id,
        reason="references recorded",
    )
    return snapshot


def recover_workspace(
    store: WorkspaceStore,
    workspace_id: str,
    *,
    is_live: Callable[[str], bool] | None = None,
    worktree_manager: Any | None = None,
    emit: Callable[[str, dict[str, Any]], Any] | None = None,
    task_id: str | None = None,
) -> WorkspaceHandle:
    """Recover an orphaned workspace (record + resource agree, nothing live).

    Missing records are never silently recreated; record/reality mismatch
    persists BROKEN for a human instead of masking damage with a fresh id.
    """
    handle = store.get(workspace_id)
    if handle is None:
        raise WorkspaceError(f"unknown workspace: {workspace_id!r}; refusing silent recreate")
    if handle.state is WorkspaceState.DESTROYED:
        raise WorkspaceError(f"workspace destroyed: {workspace_id!r}")
    live = _live_runs(handle, is_live)
    if live:
        raise WorkspaceBusyError(f"workspace has live runs, cannot recover: {live}")
    recovering = _touch(handle, state=WorkspaceState.RECOVERING)
    store.put(recovering)
    _emit(
        emit,
        "workspace.recovering",
        recovering,
        previous_state=handle.state.value,
        task_id=task_id,
        reason="orphan re-verification",
    )
    changed_paths: tuple[str, ...] = ()
    branch: str | None = None
    try:
        if handle.kind is WorkspaceKind.GIT_WORKTREE:
            manager = _resolve_manager(handle.kind, handle, worktree_manager)
            record = manager.status(path=handle.root)
            changed_paths = tuple(record.changed_files)
            branch = record.branch_name
        elif handle.kind is WorkspaceKind.LOCAL:
            if not Path(handle.root).is_dir():
                raise WorkspaceError(f"workspace root missing: {handle.root!r}")
        else:
            raise WorkspaceUnsupportedError(
                f"recover has no real backend for kind {handle.kind.value!r}"
            )
    except WorkspaceUnsupportedError:
        # Cannot judge without a backend: leave state untouched, never guess.
        raise
    except Exception as exc:
        broken = _touch(recovering, state=WorkspaceState.BROKEN)
        store.put(broken)
        _emit(
            emit,
            "workspace.broken",
            broken,
            previous_state="recovering",
            task_id=task_id,
            reason=f"verification failed: {exc}",
        )
        raise WorkspaceError(f"workspace recovery verification failed: {exc}") from exc
    # Proven-dead leases are pruned (liveness was just evidenced). Reaching
    # here without a checker implies no recorded runs (anything recorded
    # counts as live and was refused above), so occupancy stays as-is.
    pruned = () if is_live is not None else recovering.active_run_ids
    ready = _touch(
        recovering,
        state=WorkspaceState.READY,
        active_run_ids=pruned,
        generation=recovering.generation + 1,
    )
    # P0-05/P1-07: the re-verified workspace is a new canonical revision. The
    # mutations the orphaned run left are recorded against the base they were
    # produced from, and a projection still sitting on the superseded revision
    # is surfaced as a classified conflict — never silently re-pointed.
    ready = _record_authority(ready)
    ready = _record_mutation_set(
        ready,
        execution_id=_execution_id(recovering, task_id),
        changed_paths=changed_paths,
        git_state=None if branch is None else {"branch": branch},
    )
    ready = _reconcile_recorded_projection(
        ready,
        git_diverged=_git_diverged(ready, branch),
        path_overlap=bool(set(changed_paths) & set(_recorded_changed_paths(ready))),
    )
    store.put(ready)
    _emit(
        emit,
        "workspace.recovered",
        ready,
        previous_state="recovering",
        task_id=task_id,
        reason="verified usable",
    )
    return ready


def reset_workspace(
    store: WorkspaceStore,
    workspace_id: str,
    *,
    is_live: Callable[[str], bool] | None = None,
    emit: Callable[[str, dict[str, Any]], Any] | None = None,
    task_id: str | None = None,
    reason: str = "reset to clean baseline",
) -> WorkspaceHandle:
    """Return to a clean/known baseline marker (no file deletion here).

    Active leases deny the reset. File-level resets stay with the owning
    task flows; this only resets lifecycle state + generation.
    """
    handle = store.get(workspace_id)
    if handle is None:
        raise WorkspaceError(f"unknown workspace: {workspace_id!r}")
    if handle.state in (WorkspaceState.BROKEN, WorkspaceState.DESTROYED):
        raise WorkspaceError(f"cannot reset {handle.state.value} workspace: {workspace_id!r}")
    live = _live_runs(handle, is_live)
    if live:
        raise WorkspaceBusyError(f"workspace actively leased, reset denied: {live}")
    if is_live is not None:
        handle = _touch(handle, active_run_ids=())
    previous = handle.state.value
    handle = _touch(handle, state=WorkspaceState.READY, generation=handle.generation + 1)
    # An explicit baseline reset is the operator decision that discards a
    # diverged projection: new canonical revision, the stale view dropped and
    # the conflict evidence cleared (never repaired silently).
    handle = _record_authority(handle)
    metadata = dict(handle.metadata)
    metadata.pop(PROJECTION_METADATA_KEY, None)
    metadata.pop(PROJECTION_CONFLICT_KEY, None)
    handle = _touch(handle, metadata=metadata)
    store.put(handle)
    _emit(emit, "workspace.reset", handle, previous_state=previous, task_id=task_id, reason=reason)
    return handle


def release_workspace(
    store: WorkspaceStore,
    workspace_id: str,
    run_id: str,
    *,
    retain: bool = True,
    emit: Callable[[str, dict[str, Any]], Any] | None = None,
    task_id: str | None = None,
) -> WorkspaceHandle | None:
    """A task/run stops occupying the workspace (idempotent, harmless twice).

    Releasing an unknown workspace (e.g. pre-lifecycle legacy tasks) is a
    harmless no-op returning ``None`` — release must never fail a cancel.
    """
    handle = store.get(workspace_id)
    if handle is None:
        return None
    if run_id not in handle.active_run_ids:
        return handle
    previous = handle.state.value
    runs = tuple(run for run in handle.active_run_ids if run != run_id)
    updates: dict[str, Any] = {"active_run_ids": runs}
    if not runs and handle.state is WorkspaceState.IN_USE:
        updates["state"] = WorkspaceState.RELEASED if retain else WorkspaceState.READY
    handle = _touch(handle, **updates)
    handle = _drop_lease(handle, run_id)
    store.put(handle)
    _emit(
        emit,
        "workspace.released",
        handle,
        previous_state=previous,
        task_id=task_id,
        run_id=run_id,
        reason="run detached",
    )
    return handle


def _contained(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return path != root


def destroy_workspace(
    store: WorkspaceStore,
    workspace_id: str,
    *,
    managed_root: str | Path,
    is_live: Callable[[str], bool] | None = None,
    worktree_manager: Any | None = None,
    force: bool = False,
    emit: Callable[[str, dict[str, Any]], Any] | None = None,
    task_id: str | None = None,
) -> WorkspaceHandle:
    """Delete workspace-owned disposable resources, fail-closed.

    Guards (all must pass): no active runs, no live lease, target inside the
    Veya-managed root (never the root itself), kind with a real removal
    backend. ``DESTROYED`` is a persisted tombstone; a second destroy is a
    harmless no-op. Local repo roots are never deleted from the filesystem.
    """
    handle = store.get(workspace_id)
    if handle is None:
        raise WorkspaceError(f"unknown workspace: {workspace_id!r}")
    if handle.state is WorkspaceState.DESTROYED:
        return handle
    live = _live_runs(handle, is_live)
    if live:
        raise WorkspaceBusyError(f"workspace actively leased, destroy denied: {live}")
    if is_live is not None:
        handle = _touch(handle, active_run_ids=())
    root = Path(handle.root).expanduser().resolve()
    managed = Path(managed_root).expanduser().resolve()
    if not _contained(root, managed):
        raise WorkspacePathError(f"workspace root escapes managed root: {handle.root!r}")
    if (root / ".git").exists() and handle.kind is WorkspaceKind.LOCAL:
        raise WorkspacePathError(f"refusing to delete a repository root: {handle.root!r}")
    if handle.kind is WorkspaceKind.GIT_WORKTREE:
        manager = _resolve_manager(handle.kind, handle, worktree_manager)
        bound_task = handle.metadata.get("task_id")
        if not bound_task:
            raise WorkspaceError("git_worktree destroy needs metadata.task_id binding")
        manager.discard(bound_task, force=force)
    elif handle.kind is WorkspaceKind.LOCAL:
        # Mounts/shared repo roots: retire the record, touch nothing on disk.
        pass
    else:
        raise WorkspaceUnsupportedError(
            f"destroy has no real backend for kind {handle.kind.value!r}"
        )
    previous = handle.state.value
    handle = _touch(handle, state=WorkspaceState.DESTROYED, active_run_ids=())
    # The projection an execution ran against ends with the workspace.
    projection = _projection_of(handle)
    if projection is not None:
        handle = _with_metadata(
            handle,
            PROJECTION_METADATA_KEY,
            replace(
                projection,
                state=ProjectionState.DESTROYED,
                last_reconciled_at=time.time(),
            ).to_dict(),
        )
    handle = _drop_leases(handle)
    store.put(handle)
    _emit(
        emit,
        "workspace.destroyed",
        handle,
        previous_state=previous,
        task_id=task_id,
        reason="disposable resources removed",
    )
    return handle


def collect_garbage(
    store: WorkspaceStore,
    *,
    managed_root: str | Path,
    is_live: Callable[[str], bool] | None = None,
    worktree_manager_factory: Callable[[], Any] | None = None,
    max_age_s: float | None = None,
    emit: Callable[[str, dict[str, Any]], Any] | None = None,
) -> dict[str, Any]:
    """Bounded GC over released disposable workspaces (never active ones).

    Eligible: ``RELEASED`` + ``git_worktree`` kind + contained path.
    ``BROKEN`` is reported, never auto-deleted. An optional age bound
    applies to ``RELEASED`` records only — active workspaces are never
    age-collected.
    """
    import time as _time

    now = _time.time()
    destroyed: list[str] = []
    skipped: list[dict[str, Any]] = []
    for handle in store.list():
        if handle.state is not WorkspaceState.RELEASED:
            if handle.state is WorkspaceState.BROKEN:
                skipped.append(
                    {"workspace_id": handle.workspace_id, "reason": "broken-needs-human"}
                )
            continue
        if handle.kind is not WorkspaceKind.GIT_WORKTREE:
            skipped.append({"workspace_id": handle.workspace_id, "reason": "kind-not-disposable"})
            continue
        if max_age_s is not None:
            try:
                age = now - datetime.fromisoformat(handle.last_used_at).timestamp()
            except ValueError:
                age = 0.0
            if age < max_age_s:
                skipped.append({"workspace_id": handle.workspace_id, "reason": "not-expired"})
                continue
        try:
            destroy_workspace(
                store,
                handle.workspace_id,
                managed_root=managed_root,
                is_live=is_live,
                worktree_manager=worktree_manager_factory,
                emit=emit,
            )
        except WorkspaceError as exc:
            skipped.append({"workspace_id": handle.workspace_id, "reason": str(exc)[:120]})
            continue
        destroyed.append(handle.workspace_id)
    return {"destroyed": destroyed, "skipped": skipped}
