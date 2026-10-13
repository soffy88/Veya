"""W13 → existing approval path.

W13 has **no** approval store, grant table or resolver. Every remote mutation
(start / cancel / hand off) creates a pending record through
``server.approvals.ApprovalService`` — the same authority the REST layer,
``request_handoff`` and the tool guard already use — and a human resolves it at
``POST /api/v1/approvals/{approval_id}/resolve``.

This module is the thin, flag-aware adapter around it: it stamps the W13
session id into the record so a resolved approval can be matched back to a
session, and it reads the decision back out.
"""

from __future__ import annotations

from typing import Any

#: Tool names as they appear in an approval record. They carry the remote verb
#: so an approver reading the queue can see what they are authorising.
TOOL_START = "remote_agent_start"
TOOL_CANCEL = "remote_agent_cancel"
TOOL_HANDOFF = "remote_agent_handoff"

#: The one approval decision that lets a mutation proceed.
APPROVED = "approved"
#: Terminal refusals.
DENIED_DECISIONS = frozenset({"rejected", "denied"})


class RemoteApprovalRequired(RuntimeError):
    """A remote mutation was attempted with no approved record."""

    def __init__(self, message: str, *, approval_id: str = "") -> None:
        super().__init__(message)
        self.approval_id = approval_id


def _service():
    from server.approvals import get_approval_service

    return get_approval_service()


async def request_approval(
    *,
    tool: str,
    user_id: str,
    session_id: str,
    args: dict[str, Any],
    reason: str,
    host_id: str = "",
) -> str:
    """Create one pending approval. Returns the approval id.

    The record is scoped to ``user_id``'s pseudonym exactly like every other
    approval, so it shows up in that user's queue and nowhere else.
    """

    payload = dict(args)
    payload["session_id"] = session_id
    payload["host_id"] = host_id
    record = await _service().request(
        tool=tool,
        args=payload,
        user_id=user_id,
        session_id=session_id,
        actor="remote_control",
        reason=reason,
    )
    return str(record.get("approval_id") or "")


def approval_state(approval_id: str) -> tuple[str, str]:
    """Return ``(status, decision)`` for an approval id.

    ``("", "")`` when the id is unknown. ``decision`` is ``""`` while pending
    and one of ``once`` / ``session`` / ``always`` / ``deny`` once resolved.
    """

    if not approval_id:
        return "", ""
    record = _service().get(approval_id)
    if not record:
        return "", ""
    return str(record.get("status") or ""), str(record.get("decision") or "")


def is_approved(approval_id: str) -> bool:
    """True only when the record exists, is resolved, and was not refused."""

    status, decision = approval_state(approval_id)
    if status != "resolved" or not decision:
        return False
    return decision not in DENIED_DECISIONS


def require_approved(approval_id: str) -> None:
    """Gate a remote mutation on an approved record.

    Raises :class:`RemoteApprovalRequired` for a missing, unknown, pending or
    explicitly refused record — a caller must never treat "no record" as
    "approved".
    """

    if is_approved(approval_id):
        return
    status, decision = approval_state(approval_id)
    if not approval_id:
        raise RemoteApprovalRequired("remote mutation needs an approval id")
    if status == "resolved" and decision in DENIED_DECISIONS:
        raise RemoteApprovalRequired("approval was refused", approval_id=approval_id)
    if status == "resolved":
        raise RemoteApprovalRequired(
            f"approval resolved as {decision!r}, not approved", approval_id=approval_id
        )
    raise RemoteApprovalRequired("approval is still pending", approval_id=approval_id)


def pending_for_scope(user_id: str) -> list[dict[str, Any]]:
    """Pending remote approvals for one user — powers the phone's cards."""

    from server.approvals import get_approval_service

    scope = get_approval_service().scope_for(user_id)
    remote = {TOOL_START, TOOL_CANCEL, TOOL_HANDOFF}
    return [
        record
        for record in get_approval_service().list_pending(scope=scope)
        if str(record.get("tool") or "") in remote
    ]


__all__ = [
    "APPROVED",
    "DENIED_DECISIONS",
    "TOOL_CANCEL",
    "TOOL_HANDOFF",
    "TOOL_START",
    "RemoteApprovalRequired",
    "approval_state",
    "is_approved",
    "pending_for_scope",
    "request_approval",
    "require_approved",
]
