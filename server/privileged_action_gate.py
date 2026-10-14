"""Ask the existing Action Gateway whether a privileged side effect may run.

Two REST surfaces request actions that change the world — installing a CLI on a
runner, and updating or (un)installing a runner's service unit.  Both must go
through the existing approval path, so both ask here.

This module adds no approval system.  It builds one canonical
``obase.ActionRequest`` and hands it to
:class:`~server.action_gateway_adapter.ActionGatewayAdapter`, the same policy,
audit and ledger authority the tool surface already uses, and translates the
verdict into what an HTTP caller can act on.

The distinction that matters: a REST request has no interactive approver
attached to it.  A verdict of ``APPROVAL_REQUIRED`` is therefore reported as
*pending* — the endpoint tells the caller an approval exists and performs
nothing — rather than being resolved by the endpoint itself, and it is never
read as consent.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from server.action_gateway_adapter import ActionGatewayAdapter
from veya.platform import load

VERDICT_ALLOW = "ALLOW"
VERDICT_APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
VERDICT_DENY = "DENY"

STATUS_APPROVED = "approved"
STATUS_APPROVAL_REQUIRED = "approval_required"
STATUS_DENIED = "denied"

#: HTTP status for each outcome. 202 carries the pending request id so the
#: caller can find the approval it has to settle.
HTTP_FOR_STATUS = {
    STATUS_APPROVED: 200,
    STATUS_APPROVAL_REQUIRED: 202,
    STATUS_DENIED: 403,
}


@dataclass(frozen=True)
class GateOutcome:
    """What the policy said about one requested side effect."""

    status: str
    reason: str
    request_id: str

    @property
    def http_status(self) -> int:
        return HTTP_FOR_STATUS.get(self.status, 403)

    @property
    def approved(self) -> bool:
        return self.status == STATUS_APPROVED

    def to_payload(self, **extra: Any) -> dict[str, Any]:
        return {"status": self.status, "reason": self.reason, **extra}


def _request_id(action: str, arguments: Mapping[str, Any]) -> str:
    """A stable id for one exact request, so a retry is the same request."""

    body = json.dumps(dict(arguments), sort_keys=True, ensure_ascii=False, default=str)
    digest = hashlib.sha256(f"{action}:{body}".encode()).hexdigest()[:16]
    return f"veya-privileged:{action}:{digest}"


def request_privileged_action(
    *,
    action: str,
    arguments: Mapping[str, Any] | None = None,
    resource: str = "",
    effect: str = "destructive",
    bot_id: str = "",
    goal_run_id: str = "",
    actor: str = "",
    session_id: str = "",
) -> GateOutcome:
    """Evaluate one privileged action against the existing gateway policy.

    Performs nothing and records nothing: the caller still has to run the
    action through :meth:`ActionGatewayAdapter.execute` (or a runner that does).
    """

    obase = load("obase")
    arguments = dict(arguments or {})
    request_id = _request_id(action, arguments)

    # W17: Ask-once session-scoped cache
    from server.ask_once_cache import get_ask_once_cache

    sid = session_id or goal_run_id or bot_id
    cached = get_ask_once_cache().check(sid, action, arguments, effect=effect)
    if cached is not None:
        return GateOutcome(
            status=STATUS_APPROVED,
            reason="ask-once session cache",
            request_id=request_id,
        )
    gateway = ActionGatewayAdapter(
        goal_run_id=goal_run_id or request_id,
        work_item_id=goal_run_id or request_id,
        bot_id=bot_id or "system",
    )
    request = obase.ActionRequest(
        action=str(action),
        effect=str(effect),
        resource=str(resource or action),
        arguments=arguments,
        actor=str(actor or bot_id or "user"),
        request_id=request_id,
        source="privileged_action_gate",
        context={"rest": True},
    )
    decision = gateway.evaluate_policy(request)
    verdict = str(getattr(decision, "verdict", VERDICT_DENY) or VERDICT_DENY)
    reason = str(getattr(decision, "reason", "") or "")
    if verdict == VERDICT_ALLOW:
        status = STATUS_APPROVED
    elif verdict == VERDICT_APPROVAL_REQUIRED:
        status = STATUS_APPROVAL_REQUIRED
    else:
        status = STATUS_DENIED

    # Protected configuration targets must never be auto-allowed
    from server.config_protection import is_protected_config_path

    for k in ("path", "filepath", "target", "dest", "file"):
        val = arguments.get(k)
        if val and is_protected_config_path(val):
            if status == STATUS_APPROVED:
                status = STATUS_APPROVAL_REQUIRED
                reason = "protected configuration path requires human approval"
            break

    return GateOutcome(status=status, reason=reason or verdict, request_id=request_id)


__all__ = [
    "HTTP_FOR_STATUS",
    "STATUS_APPROVAL_REQUIRED",
    "STATUS_APPROVED",
    "STATUS_DENIED",
    "VERDICT_ALLOW",
    "VERDICT_APPROVAL_REQUIRED",
    "VERDICT_DENY",
    "GateOutcome",
    "record_privileged_action_approval",
    "request_privileged_action",
]


def record_privileged_action_approval(
    *,
    action: str,
    arguments: Mapping[str, Any] | None = None,
    session_id: str = "",
    decision: str = "approved",
    approval_id: str = "",
    effect: str = "",
) -> bool:
    """Record a completed human approval in the ask-once cache."""
    from server.ask_once_cache import get_ask_once_cache

    return get_ask_once_cache().record(
        session_id,
        action,
        dict(arguments or {}),
        decision=decision,
        approval_id=approval_id,
        effect=effect,
    )
