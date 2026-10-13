"""W13 remote-control REST surface (flag ``VEYA_REMOTE_CONTROL``).

Thin wiring only: identity resolution, the approval gate, the wake policy and
the state machine all live in ``server/remote_control/*``; this module owns
auth, ownership (404-not-403) and request/response shaping.

The router is mounted unconditionally; while the flag is off every endpoint
returns 404 (``require_remote_control``), so the surface is invisible rather
than merely empty — the same pattern ``server/routes/channels.py`` uses.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from server import auth as auth_mod
from server.remote_control import approval as approval_mod
from server.remote_control import executor as executor_mod
from server.remote_control import files as files_mod
from server.remote_control import flags, hosts as hosts_mod, sessions as sessions_mod
from server.remote_control import notify as notify_mod
from server.remote_control import service as service_mod
from server.remote_control import wake as wake_mod

router = APIRouter(
    prefix="/api/v1/remote",
    tags=["remote-control"],
    dependencies=[Depends(auth_mod.require_user)],
)


async def require_remote_control() -> None:
    """Dependency: 404 unless VEYA_REMOTE_CONTROL is on (fail-closed)."""

    if not flags.enabled():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="VEYA_REMOTE_CONTROL is not enabled",
        )


def _user_id(user: dict[str, Any]) -> str:
    return str(user.get("user_id") or user.get("username") or "anonymous")


#: Refusal code → HTTP status. ``not_found`` is deliberately 404: another
#: user's record must never be confirmed to exist.
_STATUS_FOR_CODE = {
    "not_found": status.HTTP_404_NOT_FOUND,
    "identity": status.HTTP_403_FORBIDDEN,
    "disabled": status.HTTP_409_CONFLICT,
    "terminal": status.HTTP_409_CONFLICT,
    "not_cancellable": status.HTTP_409_CONFLICT,
    "no_target": status.HTTP_409_CONFLICT,
    "launch": status.HTTP_502_BAD_GATEWAY,
    "refused": status.HTTP_400_BAD_REQUEST,
}


def _http_for(exc: service_mod.RemoteControlRefused) -> HTTPException:
    return HTTPException(
        status_code=_STATUS_FOR_CODE.get(exc.code, status.HTTP_400_BAD_REQUEST),
        detail=str(exc),
    )


def _owned_session(session_id: str, user: dict[str, Any]) -> sessions_mod.RemoteSession:
    session = sessions_mod.get_session(session_id)
    if session is None or session.owner_id != _user_id(user):
        raise HTTPException(status_code=404, detail="session not found")
    return session


# --------------------------------------------------------------------------
# Hosts
# --------------------------------------------------------------------------


class HostCreateRequest(BaseModel):
    host_id: str = Field(..., pattern=r"^[a-z0-9][a-z0-9-]{0,62}$")
    display_name: str = ""
    tailnet_ip: str = Field(..., min_length=1)
    agent_label: str = "pi"
    agent_command: str = ""
    workspace_path: str = ""
    enabled: bool = True


@router.get("/hosts")
async def list_remote_hosts(
    user: dict[str, Any] = Depends(auth_mod.get_current_user),
    _: None = Depends(require_remote_control),
) -> dict[str, Any]:
    hosts = hosts_mod.list_hosts(owner_id=_user_id(user))
    return {"hosts": [host.to_dict() for host in hosts]}


@router.post("/hosts", status_code=status.HTTP_201_CREATED)
async def create_remote_host(
    payload: HostCreateRequest,
    user: dict[str, Any] = Depends(auth_mod.get_current_user),
    _: None = Depends(require_remote_control),
) -> dict[str, Any]:
    host = hosts_mod.RemoteHost(
        host_id=payload.host_id,
        display_name=payload.display_name or payload.host_id,
        tailnet_ip=payload.tailnet_ip,
        agent_command=payload.agent_command,
        agent_label=payload.agent_label,
        owner_id=_user_id(user),
        workspace_path=payload.workspace_path,
        enabled=payload.enabled,
    )
    try:
        saved = hosts_mod.save_host(host)
    except hosts_mod.HostValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return saved.to_dict()


@router.delete("/hosts/{host_id}")
async def delete_remote_host(
    host_id: str,
    user: dict[str, Any] = Depends(auth_mod.get_current_user),
    _: None = Depends(require_remote_control),
) -> dict[str, Any]:
    host = hosts_mod.get_host(host_id)
    if host is None or host.owner_id != _user_id(user):
        raise HTTPException(status_code=404, detail="host not found")
    hosts_mod.delete_host(host_id)
    return {"deleted": host_id}


# --------------------------------------------------------------------------
# Sessions
# --------------------------------------------------------------------------


class SessionStartRequest(BaseModel):
    host_id: str
    task: str = Field(..., min_length=1)
    agent_label: str = ""
    transport: str = Field("local", pattern="^(local|ssh)$")
    requested_wake: str = Field("now", pattern="^(now|batch|none)$")


class HandoffRequestBody(BaseModel):
    target_host_id: str
    target_agent_label: str = ""


class ApplyRequestBody(BaseModel):
    approval_id: str = ""


@router.get("/sessions")
async def list_remote_sessions(
    active: bool = False,
    user: dict[str, Any] = Depends(auth_mod.get_current_user),
    _: None = Depends(require_remote_control),
) -> dict[str, Any]:
    sessions = sessions_mod.list_sessions(owner_id=_user_id(user), active_only=active)
    return {"sessions": [session.to_dict() for session in sessions]}


@router.post("/sessions", status_code=status.HTTP_202_ACCEPTED)
async def start_remote_session(
    payload: SessionStartRequest,
    user: dict[str, Any] = Depends(auth_mod.get_current_user),
    _: None = Depends(require_remote_control),
) -> dict[str, Any]:
    try:
        result = await service_mod.request_start(
            host_id=payload.host_id,
            owner_id=_user_id(user),
            task=payload.task,
            agent_label=payload.agent_label,
            transport=payload.transport,
            requested_wake=payload.requested_wake,
        )
    except service_mod.RemoteControlRefused as exc:
        raise _http_for(exc) from exc
    except executor_mod.CommandRejected as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {
        "session": result["session"].to_dict(),
        "approval_id": result["approval_id"],
    }


@router.get("/sessions/{session_id}")
async def get_remote_session(
    session_id: str,
    user: dict[str, Any] = Depends(auth_mod.get_current_user),
    _: None = Depends(require_remote_control),
) -> dict[str, Any]:
    return _owned_session(session_id, user).to_dict()


@router.post("/sessions/{session_id}/apply", status_code=status.HTTP_202_ACCEPTED)
async def apply_remote_session(
    session_id: str,
    payload: ApplyRequestBody,
    user: dict[str, Any] = Depends(auth_mod.get_current_user),
    _: None = Depends(require_remote_control),
) -> dict[str, Any]:
    """Start an accepted session whose approval has been resolved as approved."""

    _owned_session(session_id, user)
    try:
        result = service_mod.apply_approved(
            session_id, approval_id=payload.approval_id, bot_id=_user_id(user)
        )
    except service_mod.RemoteControlRefused as exc:
        raise _http_for(exc) from exc
    except approval_mod.RemoteApprovalRequired as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {
        "session": result["session"].to_dict(),
        "wake": result.get("wake", {}),
        "launched": result.get("launched", False),
    }


@router.post("/sessions/{session_id}/cancel", status_code=status.HTTP_202_ACCEPTED)
async def cancel_remote_session(
    session_id: str,
    user: dict[str, Any] = Depends(auth_mod.get_current_user),
    _: None = Depends(require_remote_control),
) -> dict[str, Any]:
    try:
        result = await service_mod.request_cancel(session_id, owner_id=_user_id(user))
    except service_mod.RemoteControlRefused as exc:
        raise _http_for(exc) from exc
    return {"session_id": session_id, "approval_id": result["approval_id"]}


@router.post("/sessions/{session_id}/handoff", status_code=status.HTTP_202_ACCEPTED)
async def handoff_remote_session(
    session_id: str,
    payload: HandoffRequestBody,
    user: dict[str, Any] = Depends(auth_mod.get_current_user),
    _: None = Depends(require_remote_control),
) -> dict[str, Any]:
    try:
        result = await service_mod.request_handoff(
            session_id=session_id,
            owner_id=_user_id(user),
            target_host_id=payload.target_host_id,
            target_agent_label=payload.target_agent_label,
        )
    except service_mod.RemoteControlRefused as exc:
        raise _http_for(exc) from exc
    return {"session_id": session_id, "approval_id": result["approval_id"]}


# --------------------------------------------------------------------------
# File handoff
# --------------------------------------------------------------------------


class FileStageRequest(BaseModel):
    session_id: str
    filename: str = Field(..., min_length=1)
    size_bytes: int = Field(..., gt=0)
    sha256: str = Field(..., min_length=64, max_length=64)


@router.post("/files", status_code=status.HTTP_201_CREATED)
async def stage_remote_file(
    payload: FileStageRequest,
    user: dict[str, Any] = Depends(auth_mod.get_current_user),
    _: None = Depends(require_remote_control),
) -> dict[str, Any]:
    owner = _user_id(user)
    # The recipient is the caller: the link is bound to them and nobody else.
    try:
        record = files_mod.stage_declared(
            session_id=payload.session_id,
            recipient_id=owner,
            filename=payload.filename,
            size_bytes=payload.size_bytes,
            sha256=payload.sha256,
            owner_id=owner,
        )
    except files_mod.HandoffRejected as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    view = record.public_view()
    view["link"] = f"/api/v1/remote/files/{record.token}"
    return view


@router.get("/files/{token}")
async def fetch_remote_file(
    token: str,
    user: dict[str, Any] = Depends(auth_mod.get_current_user),
    _: None = Depends(require_remote_control),
) -> dict[str, Any]:
    record = files_mod.get_handoff(token)
    # Owner and recipient mismatch are both a plain 404 — the token is not a
    # probe for whether a record exists.
    if record is None or record.owner_id != _user_id(user):
        raise HTTPException(status_code=404, detail="attachment not available")
    if record.recipient_id != _user_id(user):
        raise HTTPException(status_code=403, detail="attachment not available")
    if record.is_expired():
        raise HTTPException(status_code=410, detail="attachment link has expired")
    return record.public_view()


# --------------------------------------------------------------------------
# Pending approvals (the phone's approve/deny cards)
# --------------------------------------------------------------------------


@router.get("/pending-approvals")
async def list_pending_remote_approvals(
    user: dict[str, Any] = Depends(auth_mod.get_current_user),
    _: None = Depends(require_remote_control),
) -> dict[str, Any]:
    """The caller's pending remote mutations, shaped for the approve/deny UI.

    Read-only over the existing approval store; resolution still goes through
    ``POST /api/v1/approvals/{id}/resolve``.
    """

    return {"cards": notify_mod.pending_approval_cards(_user_id(user))}


# --------------------------------------------------------------------------
# Wake policy
# --------------------------------------------------------------------------


class WakePolicyRequest(BaseModel):
    sender: str = Field(..., min_length=1)
    agent_label: str = Field(..., min_length=1)
    ceiling: str = Field("now", pattern="^(now|batch|none)$")
    rate_limit: int = Field(5, ge=0)
    rate_window_s: float = Field(300.0, gt=0)
    batch_interval_minutes: int = Field(15, ge=1)


def _owned_senders(user: dict[str, Any]) -> set[str]:
    return {host.host_id for host in hosts_mod.list_hosts(owner_id=_user_id(user))}


@router.get("/wake-policy")
async def get_wake_policy(
    sender: str = "",
    agent_label: str = "",
    user: dict[str, Any] = Depends(auth_mod.get_current_user),
    _: None = Depends(require_remote_control),
) -> dict[str, Any]:
    owned = _owned_senders(user)
    if sender and agent_label:
        if sender not in owned:
            raise HTTPException(status_code=404, detail="sender not found")
        return wake_mod.get_policy(sender, agent_label).to_dict()
    policies = [p.to_dict() for p in wake_mod.list_policies() if p.sender in owned]
    return {"policies": policies}


@router.put("/wake-policy")
async def set_wake_policy(
    payload: WakePolicyRequest,
    user: dict[str, Any] = Depends(auth_mod.get_current_user),
    _: None = Depends(require_remote_control),
) -> dict[str, Any]:
    if payload.sender not in _owned_senders(user):
        raise HTTPException(status_code=404, detail="sender not found")
    policy = wake_mod.WakePolicy(
        sender=payload.sender,
        agent_label=payload.agent_label,
        ceiling=payload.ceiling,
        rate_limit=payload.rate_limit,
        rate_window_s=payload.rate_window_s,
        batch_interval_minutes=payload.batch_interval_minutes,
    )
    return wake_mod.set_policy(policy).to_dict()


# --------------------------------------------------------------------------
# W17 ssh hosts (flag VEYA_SSH_HOSTS, on top of VEYA_REMOTE_CONTROL)
# --------------------------------------------------------------------------


async def require_ssh_hosts() -> None:
    """Dependency: 404 unless both W13 and W17 flags are on (fail-closed)."""

    if not flags.ssh_enabled():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="VEYA_SSH_HOSTS is not enabled")


def _owned_ssh_host(host_id: str, user: dict[str, Any]) -> hosts_mod.RemoteHost:
    host = hosts_mod.get_host(host_id)
    if host is None or host.owner_id != _user_id(user):
        raise HTTPException(status_code=404, detail="host not found")
    return host


def _ssh_backend(host: hosts_mod.RemoteHost):
    from server.remote_control import ssh_tools
    from server.remote_control.backend import SshBackend

    if not host.enabled:
        raise HTTPException(status_code=409, detail="host is disabled")
    try:
        service_mod.check_identity(host, "execute")
    except service_mod.RemoteControlRefused as exc:
        raise _http_for(exc) from exc
    return SshBackend(host, runner=ssh_tools.RUNNER, key_lookup=ssh_tools.KEY_LOOKUP)


class HostSshRequest(BaseModel):
    ssh_user: str = ""
    ssh_port: int = Field(22, ge=1, le=65535)
    ssh_key_ref: str = ""
    ssh_host_key_policy: str = Field("accept-new", pattern="^(accept-new|strict)$")
    os_family: str = Field("linux", pattern="^(linux|windows-wsl|macos)$")
    write_root: str = ""
    default_cwd: str = ""
    read_approved: bool | None = None


@router.get("/hosts/{host_id}/probe")
async def probe_ssh_host(
    host_id: str,
    user: dict[str, Any] = Depends(auth_mod.get_current_user),
    _: None = Depends(require_ssh_hosts),
) -> dict[str, Any]:
    import asyncio

    from server.remote_control import ssh as ssh_mod
    from server.remote_control import ssh_tools

    host = _owned_ssh_host(host_id, user)
    try:
        service_mod.check_identity(host, "wake")
    except service_mod.RemoteControlRefused as exc:
        raise _http_for(exc) from exc
    result = await asyncio.to_thread(ssh_mod.probe, host, runner=ssh_tools.RUNNER, key_lookup=ssh_tools.KEY_LOOKUP)
    if result.reachable:
        hosts_mod.touch_seen(host.host_id)
    return {"host_id": host.host_id, **result.to_dict()}


@router.post("/hosts/{host_id}/ssh")
async def update_ssh_host(
    host_id: str,
    payload: HostSshRequest,
    user: dict[str, Any] = Depends(auth_mod.get_current_user),
    _: None = Depends(require_ssh_hosts),
) -> dict[str, Any]:
    host = _owned_ssh_host(host_id, user)
    for key in ("ssh_user", "ssh_port", "ssh_key_ref", "ssh_host_key_policy", "os_family",
                "write_root", "default_cwd"):
        setattr(host, key, getattr(payload, key))
    if payload.read_approved is not None:
        host.metadata = {**(host.metadata or {}), "read_approved": bool(payload.read_approved)}
    try:
        saved = hosts_mod.save_host(host)
    except hosts_mod.HostValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return saved.to_dict()


@router.get("/hosts/{host_id}/files")
async def list_ssh_host_files(
    host_id: str,
    path: str = "",
    user: dict[str, Any] = Depends(auth_mod.get_current_user),
    _: None = Depends(require_ssh_hosts),
) -> dict[str, Any]:
    import asyncio

    from server.remote_control.backend import BackendError

    host = _owned_ssh_host(host_id, user)
    if not bool((host.metadata or {}).get("read_approved")):
        raise HTTPException(status_code=409, detail="host has not opted in to read without approval")
    backend = _ssh_backend(host)
    try:
        rows = await asyncio.to_thread(backend.list_dir, path)
    except BackendError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"host_id": host.host_id, "path": path, "entries": rows}


@router.get("/hosts/{host_id}/runs")
async def list_ssh_host_runs(
    host_id: str,
    user: dict[str, Any] = Depends(auth_mod.get_current_user),
    _: None = Depends(require_ssh_hosts),
) -> dict[str, Any]:
    import asyncio

    from server.remote_control.backend import BackendError

    backend = _ssh_backend(_owned_ssh_host(host_id, user))
    try:
        runs = await asyncio.to_thread(backend.list_runs)
    except BackendError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"host_id": host_id, "runs": runs}


class RunKillRequest(BaseModel):
    approval_id: str = ""


@router.post("/hosts/{host_id}/runs/{name}/kill", status_code=status.HTTP_202_ACCEPTED)
async def kill_ssh_host_run(
    host_id: str,
    name: str,
    payload: RunKillRequest,
    user: dict[str, Any] = Depends(auth_mod.get_current_user),
    _: None = Depends(require_ssh_hosts),
) -> dict[str, Any]:
    """Approval-gated: without an approved ``approval_id`` this only files one."""

    import asyncio

    from server.remote_control import ssh_tools
    from server.remote_control.backend import BackendError

    host = _owned_ssh_host(host_id, user)
    backend = _ssh_backend(host)
    blocked = await ssh_tools._gate("ssh_bg_kill", host, {"name": name}, True, payload.approval_id)
    if blocked is not None:
        return {"status": "blocked", "detail": blocked}
    try:
        detail = await asyncio.to_thread(backend.bg_kill, name)
    except BackendError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"status": "killed", "detail": detail}


__all__ = ["require_remote_control", "require_ssh_hosts", "router"]
