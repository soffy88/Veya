"""W13 remote agent sessions — the delivery state machine.

States, in strict order (``W13_REMOTE_CONTROL_SPEC.md`` §5)::

    accepted → woken → acked → done

with ``failed`` / ``cancelled`` as the non-verified terminal states and
``handed_off`` marking a session whose context moved to another agent. The four
delivery states are monotonic: a record never moves backwards, and ``done``
requires verifier evidence.

Completion evidence is never invented here. ``mark_done`` accepts a verdict
produced by the existing authority — ``server.goal_run.verify.verify_task``,
``GoalStatus.completed``, or ``server.supervisor.reporting``'s
``build_progress_report(...)["is_done"]`` — and refuses anything else.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any

from server.remote_control import _ledger

_LEDGER_ENV = "VEYA_REMOTE_CONTROL_SESSIONS_PATH"
_LEDGER_FILE = "remote_control_sessions.json"


class RemoteState(StrEnum):
    """The delivery states. Values are the wire format."""

    ACCEPTED = "accepted"
    WOKEN = "woken"
    ACKED = "acked"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"
    HANDED_OFF = "handed_off"


#: Rank of the four ordered delivery states. Terminal states have no rank.
_STATE_RANK: dict[str, int] = {
    RemoteState.ACCEPTED: 0,
    RemoteState.WOKEN: 1,
    RemoteState.ACKED: 2,
    RemoteState.DONE: 3,
}

TERMINAL_STATES: frozenset[str] = frozenset(
    {RemoteState.DONE, RemoteState.FAILED, RemoteState.CANCELLED, RemoteState.HANDED_OFF}
)

#: States in which a cancel is meaningful.
CANCELLABLE_STATES: frozenset[str] = frozenset({RemoteState.WOKEN, RemoteState.ACKED})


class TransitionRejected(RuntimeError):
    """An illegal state transition was attempted."""


@dataclass
class VerifierEvidence:
    """Proof that the work actually passed. Never self-reported."""

    verified: bool = False
    source: str = ""
    summary: str = ""
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any] | None) -> "VerifierEvidence":
        raw = value or {}
        detail = raw.get("detail")
        return cls(
            verified=bool(raw.get("verified", False)),
            source=str(raw.get("source") or ""),
            summary=str(raw.get("summary") or ""),
            detail=dict(detail) if isinstance(detail, dict) else {},
        )

    @property
    def usable(self) -> bool:
        """Evidence is usable only when verified *and* actually says something."""

        return bool(self.verified and self.source and self.summary)



def evidence_from_bg_status(status: dict[str, Any]) -> VerifierEvidence:
    """W17: turn an ``ssh_bg_status`` reading into verifier evidence.

    ``verified`` only when the remote run has finished with exit code 0 (read
    from its ``.exit`` file, never self-reported); a running, failed or lost
    run yields unusable evidence that still carries the facts.
    """

    host = str(status.get("host") or "")
    name = str(status.get("name") or "")
    code = status.get("exit_code")
    finished = bool(status.get("finished"))
    tail = str(status.get("log_tail") or "")
    last = next((line for line in reversed(tail.splitlines()) if line.strip()), "")[:200]
    return VerifierEvidence(
        verified=bool(finished and code == 0),
        source=f"ssh_bg:{host}:{name}" if host and name else "",
        summary=f"exit={code} tail={last}" if finished else "",
        detail={
            "exit_code": code,
            "finished": finished,
            "lost": bool(status.get("lost")),
            "log_tail": tail[-2000:],
        },
    )

@dataclass
class RemoteSession:
    """One remote agent run, from acceptance to a verified outcome."""

    session_id: str
    host_id: str
    agent_label: str
    task: str
    owner_id: str
    transport: str = "local"
    state: str = RemoteState.ACCEPTED
    approval_id: str = ""
    cancel_approval_id: str = ""
    handoff_approval_id: str = ""
    target_host_id: str = ""
    target_agent_label: str = ""
    requested_wake: str = "now"
    wake_outcome: str = ""
    routine_id: str = ""
    peer_subject: str = ""
    context: dict[str, Any] = field(default_factory=dict)
    handoff_from: str = ""
    handoff_to: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)
    history: list[dict[str, Any]] = field(default_factory=list)
    argv: list[str] = field(default_factory=list)
    pid: int | None = None
    note: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "RemoteSession":
        history = value.get("history")
        context = value.get("context")
        return cls(
            session_id=str(value.get("session_id") or ""),
            host_id=str(value.get("host_id") or ""),
            agent_label=str(value.get("agent_label") or ""),
            task=str(value.get("task") or ""),
            owner_id=str(value.get("owner_id") or ""),
            transport=str(value.get("transport") or "local"),
            state=str(value.get("state") or RemoteState.ACCEPTED),
            approval_id=str(value.get("approval_id") or ""),
            cancel_approval_id=str(value.get("cancel_approval_id") or ""),
            handoff_approval_id=str(value.get("handoff_approval_id") or ""),
            target_host_id=str(value.get("target_host_id") or ""),
            target_agent_label=str(value.get("target_agent_label") or ""),
            requested_wake=str(value.get("requested_wake") or "now"),
            wake_outcome=str(value.get("wake_outcome") or ""),
            routine_id=str(value.get("routine_id") or ""),
            peer_subject=str(value.get("peer_subject") or ""),
            context=dict(context) if isinstance(context, dict) else {},
            handoff_from=str(value.get("handoff_from") or ""),
            handoff_to=str(value.get("handoff_to") or ""),
            evidence=dict(value.get("evidence") or {}),
            history=[h for h in (history or []) if isinstance(h, dict)],
            argv=[str(a) for a in (value.get("argv") or [])],
            pid=value.get("pid") if isinstance(value.get("pid"), int) else None,
            note=str(value.get("note") or ""),
            created_at=float(value.get("created_at") or time.time()),
            updated_at=float(value.get("updated_at") or time.time()),
        )

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    def evidence_view(self) -> VerifierEvidence:
        return VerifierEvidence.from_dict(self.evidence)


def _sessions_path():
    return _ledger.default_path(_LEDGER_ENV, _LEDGER_FILE)


def _note(session: RemoteSession, state: str, *, detail: str = "") -> None:
    session.state = state
    session.updated_at = time.time()
    session.history.append(
        {"state": state, "at": session.updated_at, "detail": detail}
        if detail
        else {"state": state, "at": session.updated_at}
    )
    # History is a bounded trail, not an event log — this is bookkeeping, not a
    # second event store.
    if len(session.history) > 64:
        del session.history[:-64]


def create_session(
    *,
    host_id: str,
    agent_label: str,
    task: str,
    owner_id: str,
    transport: str = "local",
    requested_wake: str = "now",
    approval_id: str = "",
    peer_subject: str = "",
    context: dict[str, Any] | None = None,
    handoff_from: str = "",
    session_id: str = "",
) -> RemoteSession:
    """Record an accepted request. Nothing has run yet."""

    if not task.strip():
        raise ValueError("task text is required")
    if not host_id.strip():
        raise ValueError("host_id is required")
    if requested_wake not in {"now", "batch", "none"}:
        raise ValueError("requested_wake must be now|batch|none")
    session = RemoteSession(
        session_id=session_id or uuid.uuid4().hex[:16],
        host_id=host_id,
        agent_label=agent_label,
        task=task,
        owner_id=owner_id,
        transport=transport,
        requested_wake=requested_wake,
        approval_id=approval_id,
        peer_subject=peer_subject,
        context=dict(context or {}),
        handoff_from=handoff_from,
    )
    _note(session, RemoteState.ACCEPTED, detail="accepted")
    return _persist(session)


def get_session(session_id: str) -> RemoteSession | None:
    row = _ledger.read_map(_sessions_path()).get(str(session_id))
    return RemoteSession.from_dict(row) if row else None


def list_sessions(
    *,
    owner_id: str | None = None,
    host_id: str | None = None,
    active_only: bool = False,
) -> list[RemoteSession]:
    """List sessions, newest first."""

    rows = _ledger.read_map(_sessions_path())
    sessions = [RemoteSession.from_dict(row) for row in rows.values()]
    if owner_id is not None:
        sessions = [s for s in sessions if s.owner_id == owner_id]
    if host_id is not None:
        sessions = [s for s in sessions if s.host_id == host_id]
    if active_only:
        sessions = [s for s in sessions if not s.is_terminal]
    return sorted(sessions, key=lambda s: s.created_at, reverse=True)


def _persist(session: RemoteSession) -> RemoteSession:
    def _mutate(rows: dict[str, dict[str, Any]]):
        rows[session.session_id] = session.to_dict()
        return rows, session

    return _ledger.update(_sessions_path(), _mutate)


def _mutate_session(session_id: str, state: str, *, detail: str = "", **changes):
    found: RemoteSession | None = None

    def _mutate(rows: dict[str, dict[str, Any]]):
        nonlocal found
        row = rows.get(str(session_id))
        if row is None:
            return rows, None
        found = RemoteSession.from_dict(row)
        for key, value in changes.items():
            setattr(found, key, value)
        _note(found, state, detail=detail)
        rows[found.session_id] = found.to_dict()
        return rows, found

    _ledger.update(_sessions_path(), _mutate)
    return found


def _advance(
    session: RemoteSession | None,
    session_id: str,
    target: str,
    *,
    min_rank: int | None = None,
) -> RemoteSession | None:
    """Forward-only guard for the ordered delivery states.

    ``min_rank`` additionally refuses a skip: a transition may not jump over an
    earlier ordered state (``done`` requires ``acked``, ``acked`` requires
    ``woken``), so the four states are reached stepwise, not jumped.
    """

    if session is None:
        return None
    current = session.state
    if current == target:
        return session
    if current in TERMINAL_STATES:
        raise TransitionRejected(f"session is terminal ({current})")
    if target not in _STATE_RANK:
        raise TransitionRejected(f"{target} is not an ordered state")
    if _STATE_RANK[target] <= _STATE_RANK.get(current, -1):
        raise TransitionRejected(f"cannot move backwards: {current} → {target}")
    if min_rank is not None and _STATE_RANK.get(current, -1) < min_rank:
        raise TransitionRejected(
            f"{target} requires the session to have reached "
            f"{next(k for k, v in _STATE_RANK.items() if v == min_rank)}, got {current}"
        )
    return session


def mark_woken(
    session_id: str, *, wake_outcome: str = "woken_now", routine_id: str = ""
) -> RemoteSession:
    """Record that the receiver decided to actually wake a local agent."""

    current = get_session(session_id)
    if current is None:
        raise TransitionRejected("session not found")
    _advance(current, session_id, RemoteState.WOKEN)
    updated = _mutate_session(
        session_id,
        RemoteState.WOKEN,
        detail=wake_outcome,
        wake_outcome=wake_outcome,
        routine_id=routine_id,
    )
    if updated is None:
        raise TransitionRejected("session not found")
    return updated


def mark_acked(session_id: str, *, detail: str = "") -> RemoteSession:
    """Record positive evidence that the agent began the task."""

    current = get_session(session_id)
    if current is None:
        raise TransitionRejected("session not found")
    _advance(current, session_id, RemoteState.ACKED, min_rank=_STATE_RANK[RemoteState.WOKEN])
    updated = _mutate_session(session_id, RemoteState.ACKED, detail=detail)
    if updated is None:
        raise TransitionRejected("session not found")
    return updated


def mark_done(session_id: str, *, evidence: VerifierEvidence) -> RemoteSession:
    """Terminal success. **Refuses** without usable verifier evidence."""

    if not isinstance(evidence, VerifierEvidence) or not evidence.usable:
        raise TransitionRejected(
            "done requires verifier evidence (verified=True plus source and summary)"
        )
    current = get_session(session_id)
    if current is None:
        raise TransitionRejected("session not found")
    _advance(current, session_id, RemoteState.DONE, min_rank=_STATE_RANK[RemoteState.ACKED])
    updated = _mutate_session(
        session_id,
        RemoteState.DONE,
        detail=evidence.summary[:200],
        evidence=evidence.to_dict(),
    )
    if updated is None:
        raise TransitionRejected("session not found")
    return updated


def mark_failed(session_id: str, *, reason: str = "") -> RemoteSession:
    """Terminal failure. No verifier needed — the absence of a pass is the fact."""

    updated = _mutate_session(session_id, RemoteState.FAILED, detail=reason)
    if updated is None:
        raise TransitionRejected("session not found")
    return updated


def cancel_session(session_id: str, *, reason: str = "cancelled") -> RemoteSession:
    """Cancel a woken or acked session. Anything else is refused."""

    current = get_session(session_id)
    if current is None:
        raise TransitionRejected("session not found")
    if current.is_terminal:
        raise TransitionRejected(f"session is terminal ({current.state})")
    if current.state not in CANCELLABLE_STATES:
        raise TransitionRejected(
            f"cancel needs one of {sorted(CANCELLABLE_STATES)}, got {current.state}"
        )
    updated = _mutate_session(session_id, RemoteState.CANCELLED, detail=reason)
    if updated is None:
        raise TransitionRejected("session not found")
    return updated


def mark_handed_off(session_id: str, *, to_session_id: str, target_host_id: str) -> RemoteSession:
    """Close the source session because its context moved to another agent."""

    current = get_session(session_id)
    if current is None:
        raise TransitionRejected("session not found")
    if current.is_terminal:
        raise TransitionRejected(f"session is terminal ({current.state})")
    updated = _mutate_session(
        session_id,
        RemoteState.HANDED_OFF,
        detail=f"→ {to_session_id}",
        handoff_to=to_session_id,
        note=target_host_id,
    )
    if updated is None:
        raise TransitionRejected("session not found")
    return updated


def attach_launch(session_id: str, *, argv: list[str], pid: int | None) -> RemoteSession:
    """Record the argv/pid actually started. Not a transition — state is untouched."""

    updated: RemoteSession | None = None

    def _mutate(rows: dict[str, dict[str, Any]]):
        nonlocal updated
        row = rows.get(str(session_id))
        if row is None:
            return rows, None
        updated = RemoteSession.from_dict(row)
        updated.argv = list(argv)
        updated.pid = pid
        updated.updated_at = time.time()
        rows[updated.session_id] = updated.to_dict()
        return rows, updated

    _ledger.update(_sessions_path(), _mutate)
    if updated is None:
        raise TransitionRejected("session not found")
    return updated


def set_approval_ref(session_id: str, **refs: str) -> RemoteSession:
    """Record which approval id authorises a cancel / hand-off on this session.

    A hand-off also records the target the approver saw, so the applied
    hand-off cannot be redirected afterwards. Not a transition.
    """

    allowed = {
        "cancel",
        "handoff",
        "target_host_id",
        "target_agent_label",
    }
    unknown = set(refs) - allowed
    if unknown:
        raise ValueError(f"unknown approval ref(s): {sorted(unknown)}")

    updated: RemoteSession | None = None

    def _mutate(rows: dict[str, dict[str, Any]]):
        nonlocal updated
        row = rows.get(str(session_id))
        if row is None:
            return rows, None
        updated = RemoteSession.from_dict(row)
        if refs.get("cancel"):
            updated.cancel_approval_id = str(refs["cancel"])
        if refs.get("handoff"):
            updated.handoff_approval_id = str(refs["handoff"])
        if refs.get("target_host_id"):
            updated.target_host_id = str(refs["target_host_id"])
        if refs.get("target_agent_label"):
            updated.target_agent_label = str(refs["target_agent_label"])
        updated.updated_at = time.time()
        rows[updated.session_id] = updated.to_dict()
        return rows, updated

    _ledger.update(_sessions_path(), _mutate)
    if updated is None:
        raise TransitionRejected("session not found")
    return updated


def build_handoff_context(session: RemoteSession) -> dict[str, Any]:
    """The exact context bundle that travels to another agent.

    Only these fields travel. Nothing is copied implicitly: an unverified
    attachment reference is stripped rather than forwarded, so a downstream
    agent can never be pointed at an unverified file.
    """

    context: dict[str, Any] = {
        "task": session.task,
        "state": session.state,
        "source_session_id": session.session_id,
        "source_host_id": session.host_id,
        "source_agent_label": session.agent_label,
        "created_at": session.created_at,
        "updated_at": session.updated_at,
        "history": session.history[-16:],
    }
    evidence = session.evidence_view()
    if evidence.usable:
        context["verifier_evidence"] = {
            "source": evidence.source,
            "summary": evidence.summary,
        }
    verified_files = [
        str(item)
        for item in (session.context.get("verified_files") or [])
        if str(item).startswith("ok:")
    ]
    if verified_files:
        context["verified_files"] = verified_files
    return context


def prune_finished(*, older_than_s: float = 7 * 86400.0, now: float | None = None) -> int:
    """Drop terminal sessions older than the retention window.

    Driven from the existing routine clock seam — W13 starts no timer of its
    own.
    """

    cutoff = (time.time() if now is None else float(now)) - float(older_than_s)
    removed = 0

    def _mutate(rows: dict[str, dict[str, Any]]):
        nonlocal removed
        keep: dict[str, dict[str, Any]] = {}
        for key, row in rows.items():
            session = RemoteSession.from_dict(row)
            if session.is_terminal and session.updated_at < cutoff:
                removed += 1
                continue
            keep[key] = row
        return keep, None

    _ledger.update(_sessions_path(), _mutate)
    return removed


__all__ = [
    "CANCELLABLE_STATES",
    "TERMINAL_STATES",
    "RemoteSession",
    "RemoteState",
    "TransitionRejected",
    "VerifierEvidence",
    "attach_launch",
    "build_handoff_context",
    "cancel_session",
    "create_session",
    "get_session",
    "list_sessions",
    "mark_acked",
    "mark_done",
    "mark_failed",
    "mark_handed_off",
    "mark_woken",
    "prune_finished",
    "set_approval_ref",
]
