"""Generic workspace lifecycle (D4, Veya project runtime owner).

ONE authority for workspace identity, attachment, state, leases, recovery,
safe release/destroy decisions and audit projection. Underlying mechanisms
stay where they are — git worktree ops in ``WorktreeManager``, detection in
``workspace_detect``, sandboxes in ``sandbox_profiles`` — this module never
reimplements them and never moves them into 3O.

Frozen split (§7): file/workspace continuity != agent session continuity.
D3 decides session disposition; here we only decide whether a workspace is
reused / recovered / reset / replaced, joined by explicit refs.

The module is stdlib-only besides sibling coding substrates. Audit emission
is injected (``emit``) so no ``server/`` dependency is introduced.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from runtime.coding.worktree import WorktreeError, WorktreeManager

__all__ = [
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
        return existing
    handle = WorkspaceHandle(
        workspace_id=workspace_id,
        owner_scope=owner_scope,
        root=root,
        kind=resolved_kind,
        state=WorkspaceState.NEW,
        metadata=dict(metadata or {}),
    )
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
    if handle.kind is WorkspaceKind.GIT_WORKTREE:
        manager = _resolve_manager(handle.kind, handle, worktree_manager)
        manager.status(path=handle.root)  # raises when unregistered/missing
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
    """Record recovery references (read-only; never copies the filesystem)."""
    handle = store.get(workspace_id)
    if handle is None:
        raise WorkspaceError(f"unknown workspace: {workspace_id!r}")
    branch: str | None = None
    clean: bool | None = None
    dirty: str | None = None
    if handle.kind is WorkspaceKind.GIT_WORKTREE:
        manager = _resolve_manager(handle.kind, handle, worktree_manager)
        record = manager.status(path=handle.root)
        branch = record.branch_name
        clean = record.clean
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
    try:
        if handle.kind is WorkspaceKind.GIT_WORKTREE:
            manager = _resolve_manager(handle.kind, handle, worktree_manager)
            manager.status(path=handle.root)
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
