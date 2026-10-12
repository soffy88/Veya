"""Side-effect approval gate for the canonical (product/bot) execution path.

W11-C / S11: a bot turn whose model selects a side-effect tool (writing a file,
posting a message) must go through the *existing* user approval mechanism
before anything is physically performed.

Why this exists
-------------
The canonical path (``MasterAgent`` -> ``GoalRun`` -> ``ActionGateway``) never
consulted ``server.user_control``'s approval gate: it lives on
``server.tool_guard`` and is only reached by the direct/legacy executor.  As a
result the Gateway's own policy chain answered ``ALLOW`` for a project
mutation and the physical write happened immediately, with no pending
approval anywhere.  This module binds that same existing authority
(``server.approvals.ApprovalService``) into the canonical path through the
Gateway's own injection points (``policy_hook`` + ``approval_resolver``).  It
adds no second approval store, no scheduler and no new route: a resolution
still lands on ``POST /api/v1/approvals/{id}/resolve``.

Contract
--------
``build_side_effect_gate`` returns ``(policy_hook, approval_resolver)``.  The
 Gateway consults the hook first; a verdict of ``REQUIRE_APPROVAL`` for a
side-effect action hands the request to the resolver, which opens a pending
approval through :class:`ApprovalService` and waits for the human decision.
Read-only actions are never gated here.
"""

from __future__ import annotations

import contextlib
import logging
from typing import Any

from server.feature_flags import enabled as flag_enabled

_log = logging.getLogger("veya.bot_approval")

#: The W6 approval-gate flag. Off = the canonical path behaves exactly as
#: before, so this module is inert unless a deployment opts in.
FLAG_APPROVAL_GATES = "VEYA_APPROVAL_GATES"

#: Effects a human must approve before anything is physically performed.
#: Read-only work is deliberately absent.
_APPROVAL_EFFECTS = frozenset(
    {
        "local_write",
        "process",
        "network",
        "remote",
        "destructive",
        "privileged",
        "unknown",
    }
)

#: How long a paused action waits for the human answer before it is refused.
#: Bounded on purpose: a bot turn must never hang a worker forever.
APPROVAL_TIMEOUT_S = 300.0


def side_effect_gate_enabled() -> bool:
    """Whether the canonical path routes side effects through the approval gate."""

    try:
        return flag_enabled(FLAG_APPROVAL_GATES)
    except KeyError:
        return False


def _effect_of(request: Any) -> str:
    """The canonical effect the Gateway assigned to this request."""

    return str(getattr(request, "effect", "") or "").strip().lower()


def _action_label(request: Any) -> str:
    """A short human-facing description of the action awaiting approval."""

    tool = str(getattr(request, "action", "") or "tool")
    arguments = getattr(request, "arguments", None)
    if isinstance(arguments, dict):
        target = arguments.get("filepath") or arguments.get("path") or arguments.get("target")
        if target:
            return f"{tool} ({target})"
    return tool


def build_side_effect_gate(
    *,
    state: Any | None = None,
    tool_label: Any | None = None,
) -> tuple[Any, Any]:
    """Build the canonical path's ``(policy_hook, approval_resolver)`` pair.

    With ``VEYA_APPROVAL_GATES`` off both are ``None``, which is exactly what
    the Gateway already treats as "no project override" — so the default
    behaviour and every existing test are unchanged.
    """

    if not side_effect_gate_enabled():
        return None, None

    def policy_hook(request: Any) -> Any:
        """Ask for a human decision on any non-read effect."""

        from veya.platform import load

        effect = _effect_of(request)
        if effect == "read":
            return None
        if effect not in _APPROVAL_EFFECTS:
            return None
        obase = load("obase")
        return obase.ActionDecision(
            verdict="REQUIRE_APPROVAL",
            reason=f"「{_action_label(request)}」需要你批准后才会执行",
            request_id=getattr(request, "request_id", ""),
        )

    async def approval_resolver(request: Any) -> bool:
        """Open a pending approval on the existing path and await the verdict.

        ``True`` means the user approved and the Gateway may perform the
        action; ``False`` means denied/timed out, which the Gateway turns into
        a normal ``DENY`` — no physical execution either way.
        """

        from server.approvals import get_approval_service
        from server.auth import current_user
        from server.user_control import _session_id

        service = get_approval_service()
        user_id = str(current_user().get("user_id") or "anonymous")
        session_id = _session_id.get()
        record = await service.request(
            tool=_action_label(request),
            args=_safe_arguments(request),
            user_id=user_id,
            session_id=session_id,
            reason=f"bot 要求执行「{_action_label(request)}」",
        )
        approval_id = str(record["approval_id"])
        _remember(state, approval_id, request)
        approved = await _await_decision(service, approval_id)
        _mark_decided(state, approval_id, approved)
        return approved

    return policy_hook, approval_resolver


def _safe_arguments(request: Any) -> dict[str, Any]:
    """Redacted arguments, so a secret never lands in the approval record."""

    arguments = getattr(request, "arguments", None)
    if not isinstance(arguments, dict):
        return {}
    try:
        from veya.platform import load

        obase = load("obase")
        return dict(obase.redact_value(dict(arguments)))
    except Exception:  # pragma: no cover - redaction is best-effort by design
        return dict(arguments)


def _remember(state: Any | None, approval_id: str, request: Any) -> None:
    """Record the pending approval in the GoalRun's durable budget.

    This is what lets a caller answer "waiting for approval" and lets a later
    resume find the approval again.  It is continuation state, not a second
    approval store: the record itself lives in ``ApprovalService``.
    """

    if state is None:
        return
    budget = getattr(state, "budget", None)
    if not isinstance(budget, dict):
        return
    budget["pending_approval"] = {
        "approval_id": approval_id,
        "tool": _action_label(request),
        "goal_run_id": str(getattr(state, "goal_id", "") or ""),
        "status": "pending",
    }


def _mark_decided(state: Any | None, approval_id: str, approved: bool) -> None:
    """Record the human's verdict in the GoalRun's continuation state."""

    if state is None:
        return
    budget = getattr(state, "budget", None)
    if not isinstance(budget, dict):
        return
    record = budget.get("pending_approval")
    if isinstance(record, dict) and str(record.get("approval_id") or "") == str(approval_id):
        record["status"] = "approved" if approved else "denied"
        record["decided"] = approved


async def _await_decision(service: Any, approval_id: str) -> bool:
    """Poll the durable approval until a human decides or the budget runs out."""

    import asyncio
    import time

    from server.approvals import wake_pending_approval

    started = time.monotonic()
    while time.monotonic() - started < APPROVAL_TIMEOUT_S:
        record = service.get(approval_id)
        if record is None:
            return False
        status = str(record.get("status") or "")
        if status == "pending":
            # The durable store is the authority; the event only shortens the
            # wait when the decision lands in this same process.
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(_approval_event(approval_id).wait(), timeout=2.0)
            continue
        decision = str(record.get("decision") or "")
        wake_pending_approval(approval_id)
        return decision in {"once", "session", "always"}
    return False


_events: dict[str, Any] = {}


def _approval_event(approval_id: str) -> Any:
    """One in-process wake-up per pending approval (best-effort fast path)."""

    import asyncio

    event = _events.get(approval_id)
    if event is None:
        event = asyncio.Event()
        _events[approval_id] = event
    return event


__all__ = [
    "APPROVAL_TIMEOUT_S",
    "build_side_effect_gate",
    "side_effect_gate_enabled",
]
