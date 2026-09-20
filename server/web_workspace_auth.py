"""Per-user workspace authorization for the Web supervision surface.

Reuses the canonical :class:`veya.remote.workspace_policy.WorkspacePolicy` for
*path* authorization only — canonicalisation, refusal of ``..``/symlink escapes,
forbidden roots, must-exist checks. It deliberately does **not** reuse the Remote
MCP token identity model: the Web identity comes from ``server.auth`` and mission
ownership is recorded on the Mission itself.

Every check fails closed: no user, no authorized root, unknown owner, or a
mission whose workspace is no longer authorized all end in a denial.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from veya.remote.models import RemotePermissions
from veya.remote.workspace_policy import WorkspacePolicy, WorkspacePolicyError

#: os.pathsep-separated list of roots this deployment authorizes for Web missions.
ALLOWED_ROOTS_ENV = "VEYA_WEB_WORKSPACE_ROOTS"
#: Mission.authority key holding the owning Web user id.
OWNER_KEY = "owner_user_id"


class WorkspaceDenied(Exception):
    """Authorization failure (mapped to HTTP 403)."""


class MissionMissing(Exception):
    """Mission does not exist under any authorized root (mapped to HTTP 404)."""


def _web_permissions() -> RemotePermissions:
    """Path authorization only: the runtime executes work, not the Web caller."""
    return RemotePermissions(read=True, write=True)


def allowed_roots() -> tuple[Path, ...]:
    raw = os.environ.get(ALLOWED_ROOTS_ENV) or os.environ.get("VEYA_WORKSPACE") or os.getcwd()
    roots: list[Path] = []
    for item in str(raw).split(os.pathsep):
        item = item.strip()
        if not item:
            continue
        candidate = Path(item).expanduser()
        try:
            resolved = candidate.resolve(strict=True)
        except OSError:
            continue
        if resolved.is_dir() and resolved not in roots:
            roots.append(resolved)
    return tuple(roots)


def user_id(user: dict[str, Any] | None) -> str:
    uid = str((user or {}).get("user_id") or "").strip()
    if not uid or uid == "anonymous":
        raise WorkspaceDenied("unauthenticated")
    return uid


def authorize_workspace(user: dict[str, Any] | None, workspace: str | None) -> str:
    """Return the canonical absolute workspace path, or deny.

    Raises:
        WorkspaceDenied: unauthenticated, no authorized roots, path escapes an
            authorized root (``..``/symlink/absolute), or the path is forbidden.
    """
    user_id(user)  # fail closed before touching the filesystem
    roots = allowed_roots()
    if not roots:
        raise WorkspaceDenied("no authorized workspace root configured")
    policy = WorkspacePolicy(root=roots[0], permissions=_web_permissions(), extra_roots=roots[1:])
    try:
        resolved = policy.resolve(workspace or roots[0], must_exist=True)
    except WorkspacePolicyError as exc:
        raise WorkspaceDenied(str(exc)) from exc
    return str(resolved)


def stamp_owner(mission: Any, uid: str) -> None:
    """Record the owning Web user on the Mission (orchestration metadata only)."""
    authority = getattr(mission, "authority", None)
    if authority is None:
        mission.authority = {}
        authority = mission.authority
    authority[OWNER_KEY] = uid


def owner_of(mission: Any) -> str:
    return str(((getattr(mission, "authority", None) or {}).get(OWNER_KEY)) or "").strip()


def _store_roots(user: dict[str, Any] | None) -> list[Path]:
    """Authorized project roots: each allowed root plus its authorized direct subdirs.

    A mission's store root is the workspace it was created in
    (``veya_mission_create(project_root=workspace)``), so both levels are searched.
    """
    user_id(user)
    roots: list[Path] = []
    for root in allowed_roots():
        roots.append(root)
        try:
            children = sorted(entry for entry in root.iterdir() if entry.is_dir())
        except OSError:
            children = []
        for child in children:
            if child.name.startswith("."):
                continue
            try:
                authorize_workspace(user, str(child))
            except WorkspaceDenied:
                continue
            roots.append(child)
    return roots


def mission_for_user(mission_id: str, user: dict[str, Any] | None) -> tuple[str, Any, Any]:
    """Load a mission the user owns; re-authorizes its workspace every time.

    Returns (store_root, mission, store). Raises MissionMissing (404) when the id
    exists nowhere the user may look, and WorkspaceDenied (403) when the mission
    belongs to someone else or its workspace is no longer authorized.
    """
    uid = user_id(user)
    from veya.supervision import MissionStore

    for root in _store_roots(user):
        store = MissionStore(str(root))
        mission = store.load(mission_id)
        if mission is None:
            continue
        if owner_of(mission) != uid:
            raise WorkspaceDenied("mission belongs to another user")
        authorize_workspace(user, getattr(mission, "workspace", "") or str(root))
        return str(root), mission, store
    raise MissionMissing(f"unknown mission: {mission_id}")


def missions_for_user(user: dict[str, Any] | None) -> list[dict[str, Any]]:
    """All missions owned by this user across authorized roots."""
    uid = user_id(user)
    from veya.supervision import MissionStore

    out: dict[str, dict[str, Any]] = {}
    for root in _store_roots(user):
        for mission in MissionStore(str(root)).list():
            if owner_of(mission) != uid:
                continue
            try:
                authorize_workspace(user, getattr(mission, "workspace", "") or str(root))
            except WorkspaceDenied:
                continue
            out[mission.mission_id] = mission.to_dict()
    return [out[key] for key in sorted(out)]


__all__ = [
    "ALLOWED_ROOTS_ENV",
    "OWNER_KEY",
    "MissionMissing",
    "WorkspaceDenied",
    "allowed_roots",
    "authorize_workspace",
    "mission_for_user",
    "missions_for_user",
    "owner_of",
    "stamp_owner",
    "user_id",
]
