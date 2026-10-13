"""W13 remote-control service — the orchestration every caller shares.

REST routes and model-callable tools both go through here so the approval
gate, the wake policy, the identity policy and the state machine cannot drift
between the two surfaces.

Order of checks on every mutating path (never reordered):

1. the feature flag;
2. host ownership + enabled state;
3. peer identity resolution and the local identity policy (``wake``/``execute``);
4. an approval record in the existing approval store, resolved as approved;
5. the receiver's wake decision;
6. only then, an executor launch.
"""

from __future__ import annotations

from typing import Any

from server.remote_control import (
    approval as approval_mod,
    executor as executor_mod,
    flags,
    hosts as hosts_mod,
    identity as identity_mod,
    notify,
    sessions as sessions_mod,
    wake as wake_mod,
)
from server.remote_control.sessions import (
    RemoteSession,
    TransitionRejected,
    VerifierEvidence,
)

#: Identity-policy action required for each remote verb.
_ACTION_FOR = {
    "start": "execute",
    "cancel": "execute",
    "handoff": "execute",
}


class RemoteControlRefused(RuntimeError):
    """A request was refused by policy, identity or approval. Safe to show."""

    def __init__(self, message: str, *, code: str = "refused") -> None:
        super().__init__(message)
        self.code = code


def _require_host(host_id: str, owner_id: str) -> hosts_mod.RemoteHost:
    host = hosts_mod.get_host(host_id)
    if host is None or host.owner_id != owner_id:
        raise RemoteControlRefused("host not found", code="not_found")
    if not host.enabled:
        raise RemoteControlRefused("host is disabled", code="disabled")
    return host


def check_identity(host: hosts_mod.RemoteHost, action: str) -> identity_mod.PeerIdentity:
    """Resolve the host's peer identity and consult the local policy."""

    identity = identity_mod.resolve_identity(host.tailnet_ip)
    allowed, reason = identity_mod.evaluate(identity, _ACTION_FOR.get(action, action))
    if not allowed:
        raise RemoteControlRefused(f"identity refused: {reason}", code="identity")
    return identity


def request_start(
    *,
    host_id: str,
    owner_id: str,
    task: str,
    agent_label: str = "",
    transport: str = "local",
    requested_wake: str = "now",
    create_approval: bool = True,
) -> dict[str, Any]:
    """Accept a task: check policy, record the session, ask for approval.

    Returns the accepted session plus, when ``create_approval`` is on, the
    pending approval id. Nothing is executed here — a start needs a human.
    """

    flags.require_enabled()
    host = _require_host(host_id, owner_id)
    identity = check_identity(host, "start")
    # Fail fast on an unsafe task before anything is persisted.
    executor_mod.render_command(host.command_template(), task=task, workspace=host.workspace_path)

    session = sessions_mod.create_session(
        host_id=host.host_id,
        agent_label=agent_label or host.agent_label,
        task=task,
        owner_id=owner_id,
        transport=transport,
        requested_wake=requested_wake,
        peer_subject=identity.subject,
    )
    if not create_approval:
        return {"session": session, "approval_id": "", "identity": identity.to_dict()}

    approval_id = _request(
        tool=approval_mod.TOOL_START,
        owner_id=owner_id,
        session_id=session.session_id,
        host_id=host.host_id,
        args={"task": task, "agent_label": session.agent_label, "transport": transport},
        reason=f"start {session.agent_label} on {host.host_id}",
    )
    session.approval_id = approval_id
    notify.notify_needs_approval(
        owner_id=owner_id,
        session_id=session.session_id,
        host_id=host.host_id,
        agent_label=session.agent_label,
        action="start",
        approval_id=approval_id,
    )
    return {
        "session": sessions_mod.get_session(session.session_id) or session,
        "approval_id": approval_id,
        "identity": identity.to_dict(),
    }


async def _request(
    *,
    tool: str,
    owner_id: str,
    session_id: str,
    host_id: str,
    args: dict[str, Any],
    reason: str,
) -> str:
    return await approval_mod.request_approval(
        tool=tool,
        user_id=owner_id,
        session_id=session_id,
        args=args,
        reason=reason,
        host_id=host_id,
    )


def advance(
    session_id: str,
    *,
    approval_id: str = "",
    bot_id: str = "",
    urgent: bool = False,
    quiet_settings: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run an accepted session: require approval, decide the wake, execute.

    Refuses unless the approval is resolved **and** approved, then applies the
    receiver's wake policy before touching a process.
    """

    flags.require_enabled()
    session = sessions_mod.get_session(session_id)
    if session is None:
        raise RemoteControlRefused("session not found", code="not_found")
    if session.is_terminal:
        raise RemoteControlRefused(f"session is terminal ({session.state})", code="terminal")

    approval_mod.require_approved(approval_id or session.approval_id)
    host = _require_host(session.host_id, session.owner_id)

    decision, policy = wake_mod.decide(
        sender=session.peer_subject or session.host_id,
        agent_label=session.agent_label,
        requested=session.requested_wake,
        urgent=urgent,
        quiet_settings=quiet_settings,
    )
    if decision.outcome == "rejected":
        return {
            "session": session,
            "wake": decision.to_dict(),
            "launched": False,
            "refused": True,
        }

    if decision.outcome == "batched":
        routine_id, minutes = wake_mod.materialise_batch(
            policy, bot_id=bot_id or session.owner_id, project_root=host.workspace_path
        )
        updated = sessions_mod.mark_woken(
            session_id, wake_outcome="batched", routine_id=routine_id
        )
        return {
            "session": updated,
            "wake": decision.to_dict(),
            "launched": False,
            "routine": {"routine_id": routine_id, "every_minutes": minutes},
        }

    result = _launch(session, host)
    wake_mod.record_wake(policy, routine_id=policy.routine_id)
    hosts_mod.touch_seen(host.host_id)
    return {
        "session": sessions_mod.get_session(session_id),
        "wake": decision.to_dict(),
        "launched": result.ok,
        "launch": result.to_dict(),
        "refused": False,
    }


def _launch(session: RemoteSession, host: hosts_mod.RemoteHost) -> executor_mod.LaunchResult:
    """Start the executor and move the session to ``acked`` on real evidence."""
    result = executor_mod.launch(
        host,
        task=session.task,
        workspace=host.workspace_path,
        transport=session.transport,
    )
    sessions_mod.attach_launch(
        session.session_id, argv=list(result.argv), pid=result.pid
    )
    if result.pid is None and result.returncode != 0:
        sessions_mod.mark_failed(
            session.session_id, reason="executor could not be started"
        )
        raise RemoteControlRefused("executor could not be started", code="launch")
    sessions_mod.mark_woken(session.session_id, wake_outcome="woken_now")
    sessions_mod.mark_acked(
        session.session_id,
        detail=(
            f"executor started (pid={result.pid})"
            if result.pid is not None
            else "executor exited cleanly"
        ),
    )
    return result


def complete(session_id: str, *, evidence: VerifierEvidence) -> RemoteSession:
    """Mark a session done — only with usable verifier evidence — and notify."""

    flags.require_enabled()
    session = sessions_mod.mark_done(session_id, evidence=evidence)
    notify.notify_done(
        owner_id=session.owner_id,
        session_id=session.session_id,
        agent_label=session.agent_label,
        host_id=session.host_id,
        summary=evidence.summary,
    )
    return session


def fail(session_id: str, *, reason: str) -> RemoteSession:
    """Mark a session failed and tell the owner."""

    flags.require_enabled()
    session = sessions_mod.mark_failed(session_id, reason=reason)
    notify.notify_cancelled(
        owner_id=session.owner_id,
        session_id=session.session_id,
        host_id=session.host_id,
        reason=reason or "failed",
    )
    return session


def request_cancel(session_id: str, *, owner_id: str) -> dict[str, Any]:
    """Cancel a remote run — through the approval path, like every mutation."""

    flags.require_enabled()
    session = sessions_mod.get_session(session_id)
    if session is None or session.owner_id != owner_id:
        raise RemoteControlRefused("session not found", code="not_found")
    if session.state not in sessions_mod.CANCELLABLE_STATES:
        raise RemoteControlRefused(
            f"nothing to cancel in state {session.state}", code="not_cancellable"
        )
    host = _require_host(session.host_id, owner_id)
    check_identity(host, "cancel")
    approval_id = _sync_request(
        tool=approval_mod.TOOL_CANCEL,
        owner_id=owner_id,
        session_id=session.session_id,
        host_id=host.host_id,
        args={"reason": "operator cancel"},
        reason=f"cancel {session.agent_label} on {host.host_id}",
    )
    notify.notify_needs_approval(
        owner_id=owner_id,
        session_id=session.session_id,
        host_id=host.host_id,
        agent_label=session.agent_label,
        action="cancel",
        approval_id=approval_id,
    )
    return {"session": session, "approval_id": approval_id}


def cancel(session_id: str, *, approval_id: str) -> RemoteSession:
    """Apply an approved cancel."""

    flags.require_enabled()
    session = sessions_mod.get_session(session_id)
    if session is None:
        raise RemoteControlRefused("session not found", code="not_found")
    approval_mod.require_approved(approval_id or session.approval_id)
    updated = sessions_mod.cancel_session(session_id, reason="cancelled by owner")
    notify.notify_cancelled(
        owner_id=updated.owner_id,
        session_id=updated.session_id,
        host_id=updated.host_id,
        reason="cancelled",
    )
    return updated


def request_handoff(
    *,
    session_id: str,
    owner_id: str,
    target_host_id: str,
    target_agent_label: str = "",
) -> dict[str, Any]:
    """Ask for approval to move a session's context to another agent."""

    flags.require_enabled()
    session = sessions_mod.get_session(session_id)
    if session is None or session.owner_id != owner_id:
        raise RemoteControlRefused("session not found", code="not_found")
    if session.is_terminal:
        raise RemoteControlRefused(f"session is terminal ({session.state})", code="terminal")
    target = _require_host(target_host_id, owner_id)
    check_identity(target, "handoff")
    approval_id = _sync_request(
        tool=approval_mod.TOOL_HANDOFF,
        owner_id=owner_id,
        session_id=session.session_id,
        host_id=target.host_id,
        args={
            "target_host_id": target.host_id,
            "target_agent_label": target_agent_label or target.agent_label,
        },
        reason=f"hand {session.agent_label}@{session.host_id} to "
        f"{target.agent_label}@{target.host_id}",
    )
    notify.notify_needs_approval(
        owner_id=owner_id,
        session_id=session.session_id,
        host_id=target.host_id,
        agent_label=target_agent_label or target.agent_label,
        action="handoff",
        approval_id=approval_id,
    )
    return {"session": session, "approval_id": approval_id}


def handoff(
    session_id: str,
    *,
    approval_id: str,
    target_host_id: str,
    target_agent_label: str = "",
) -> RemoteSession:
    """Apply an approved hand-off. The new session starts at ``accepted``."""

    flags.require_enabled()
    session = sessions_mod.get_session(session_id)
    if session is None:
        raise RemoteControlRefused("session not found", code="not_found")
    approval_mod.require_approved(approval_id)
    context = sessions_mod.build_handoff_context(session)
    created = sessions_mod.create_session(
        host_id=target_host_id,
        agent_label=target_agent_label or session.agent_label,
        task=session.task,
        owner_id=session.owner_id,
        transport=session.transport,
        requested_wake=session.requested_wake,
        context=context,
        handoff_from=session.session_id,
    )
    return sessions_mod.mark_handed_off(
        session_id, to_session_id=created.session_id, target_host_id=target_host_id
    )


def _sync_request(
    *,
    tool: str,
    owner_id: str,
    session_id: str,
    host_id: str,
    args: dict[str, Any],
    reason: str,
) -> str:
    """Create a pending approval from synchronous code (REST / tool path)."""

    import asyncio

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(
            _request(
                tool=tool,
                owner_id=owner_id,
                session_id=session_id,
                host_id=host_id,
                args=args,
                reason=reason,
            )
        )
    raise RemoteControlRefused(
        "approval must be requested from async context on this path",
        code="async_required",
    )


__all__ = [
    "RemoteControlRefused",
    "advance",
    "cancel",
    "check_identity",
    "complete",
    "fail",
    "handoff",
    "request_cancel",
    "request_handoff",
    "request_start",
]
