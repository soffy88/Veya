"""NANO-W6 approval gates: service + REST (thin layer over the 3O flows).

This module is the project service layer for approvals: it owns routes, auth
and the push relay, and delegates all reusable logic to the 3O layers
(``veya/omodul/approval_flow.py`` over ``veya/oprim/approval.py`` over
``veya/obase/approvals.py``). It adds no second approval store and no
scheduler: approvals persist in the ordinary durable store, the wait loop
polls it (so any runner holding the run can be resumed from any process),
and pushes reuse the P5 notification centre.

Guard L: an approval may be resolved by a human principal unless the
approval's delegate policy explicitly allows a specific agent; an
agent/coordinator principal attempting to approve its own or a sibling's
pending approval is rejected and recorded in the decision trail. High-risk
classes (DB migrations, payment/secret access, production) are human-only
regardless of delegation.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from server import auth as auth_mod
from server.events import append_canonical_event, current_task_id
from server.feature_flags import enabled as flag_enabled
from veya.obase.approvals import ApprovalStore
from veya.oskill.pure.permission import HUMAN_ONLY_MARKERS

_lg = logging.getLogger("veya.approvals")

FLAG_APPROVAL_GATES = "VEYA_APPROVAL_GATES"

#: High-risk tool classes are human-only regardless of delegation policy.
HUMAN_ONLY_MARKERS_TUPLE = tuple(sorted(HUMAN_ONLY_MARKERS))

router = APIRouter(prefix="/api/v1", tags=["approvals"])

# ── in-process resume channels ────────────────────────────────────────
# The wait loop polls the durable store (cross-process resume), and these
# events wake it immediately when a resolution lands in this process.

_pending_waiters: dict[str, asyncio.Event] = {}
_pending_handoffs: dict[str, asyncio.Event] = {}

#: "allow for this session" grants: (scope, tool). In-memory on purpose —
#: a session grant dies with the process, matching its semantics.
session_grants: set[tuple[str, str]] = set()


def approval_gates_enabled() -> bool:
    """The W6 gate flag. Default off: behaviour is exactly as before."""

    try:
        return flag_enabled(FLAG_APPROVAL_GATES)
    except KeyError:
        return False


def wake_pending_approval(request_id: str) -> None:
    event = _pending_waiters.get(str(request_id))
    if event is not None:
        event.set()


def wake_pending_handoff(handoff_id: str) -> None:
    event = _pending_handoffs.get(str(handoff_id))
    if event is not None:
        event.set()


def _trail(topic: str, payload: dict[str, Any], *, actor: str) -> None:
    try:
        append_canonical_event(
            topic,
            payload,
            actor=actor,
            task_id=current_task_id(),
        )
    except Exception:
        _lg.warning("approval trail append failed", exc_info=True)


class ApprovalService:
    """The approval gate service: request / resolve / grants / handoff."""

    def __init__(self, *, store: ApprovalStore | None = None) -> None:
        self.store = store if store is not None else ApprovalStore()

    @staticmethod
    def scope_for(user_id: str) -> str:
        """Pseudonym for a user id — no raw user id in records or trails."""

        return hashlib.sha256(str(user_id).encode("utf-8")).hexdigest()[:16]

    # ── request / resolve ──────────────────────────────────────────────
    async def request(
        self,
        *,
        tool: str,
        args: dict[str, Any],
        user_id: str,
        session_id: str = "",
        actor: str = "master",
        reason: str = "",
        delegate_policy: str = "",
    ) -> dict[str, Any]:
        """Create a pending approval, fire the push relay, trail it."""

        from veya.omodul.approval_flow import ApprovalFlowConfig, run_approval_flow

        scope = self.scope_for(user_id)

        def _notify(payload: dict[str, Any]) -> None:
            from server.notification_center import WEB_PUSH_FLAG, global_notifier

            body = payload.get("payload") or {}
            global_notifier.push(
                str(payload.get("type") or "APPROVAL_REQUIRED"),
                str(payload.get("title") or "Approval required"),
                str(payload.get("content") or ""),
                payload={
                    "url": str(body.get("deep_link") or "/approvals"),
                    "tag": "approval",
                    "kind": "approval",
                    "approval_id": str(body.get("approval_id") or ""),
                    WEB_PUSH_FLAG: True,
                },
                user_id=str(user_id),
            )

        config = ApprovalFlowConfig(
            store=self.store,
            notify=_notify,
            trail=lambda topic, payload: _trail(topic, payload, actor=actor),
            scope=scope,
            session_id=session_id,
            actor=actor,
        )
        result = await run_approval_flow(
            config,
            {
                "op": "request",
                "tool": tool,
                "tool_args": args,
                "reason": reason,
                "delegate_policy": delegate_policy,
            },
            "/tmp/veya-approvals",
        )
        if result.get("status") != "completed":
            raise RuntimeError(str(result.get("error") or "approval request failed"))
        return result

    def get(self, approval_id: str) -> dict[str, Any] | None:
        return self.store.get_approval(str(approval_id))

    def list_pending(self, *, scope: str = "") -> list[dict[str, Any]]:
        return self.store.list_approvals(scope=scope, status="pending")

    def list_grants(self, *, scope: str = "") -> list[dict[str, Any]]:
        return self.store.list_grants(scope=scope)

    def revoke_grant(self, grant_id: int, *, scope: str) -> bool:
        for grant in self.store.list_grants(scope=scope):
            if int(grant["id"]) == int(grant_id):
                return self.store.revoke_grant(grant_id)
        return False

    # ── guard L ───────────────────────────────────────────────────────
    def check_ask_once(
        self, session_id: str, tool: str, args: dict[str, Any] | None = None
    ) -> dict[str, Any] | None:
        from server.ask_once_cache import get_ask_once_cache

        return get_ask_once_cache().check(session_id, tool, args)

    def check_resolver_principal(self, approval: dict[str, Any], principal: str) -> str | None:
        """None when ``principal`` may resolve; otherwise the refusal reason."""

        tool = str(approval.get("tool") or "").lower()
        approval_scope = str(approval.get("scope") or "")
        scoped_human = False
        if principal.startswith("human:") and approval_scope:
            scoped_human = hmac.compare_digest(principal.partition(":")[2], approval_scope)
        if principal == "human" or scoped_human:
            return None
        if any(marker in tool for marker in HUMAN_ONLY_MARKERS_TUPLE):
            return "high-risk approvals are human-only"
        from server.config_protection import is_protected_config_path

        args = approval.get("tool_args") or {}
        if isinstance(args, dict):
            for k in ("path", "filepath", "target", "dest", "file"):
                val = args.get(k)
                if val and is_protected_config_path(val):
                    return "protected configuration changes are human-only"
        if principal.startswith("agent:"):
            if str(approval.get("delegate_policy") or "") == f"allow:{principal}":
                return None
            return "agent principal may not resolve this approval"
        return "unknown resolver principal"

    async def resolve(
        self,
        *,
        approval_id: str,
        decision: str,
        reason: str = "",
        principal: str = "human",
    ) -> dict[str, Any]:
        """Resolve one pending approval (guard L enforced)."""

        from veya.omodul.approval_flow import ApprovalFlowConfig, run_approval_flow

        record = self.store.get_approval(str(approval_id))
        if record is None:
            raise HTTPException(status_code=404, detail="approval not found")
        if record.get("status") != "pending":
            raise HTTPException(status_code=409, detail="approval already resolved")
        refusal = self.check_resolver_principal(record, principal)
        if refusal is not None:
            _trail(
                "approval.delegation_rejected",
                {
                    "request_id": record["request_id"],
                    "tool": record["tool"],
                    "principal": principal,
                    "reason": refusal,
                },
                actor=principal,
            )
            raise HTTPException(status_code=403, detail=refusal)
        config = ApprovalFlowConfig(
            store=self.store,
            trail=lambda topic, payload: _trail(topic, payload, actor=principal),
            scope=str(record.get("scope") or ""),
            session_id=str(record.get("session_id") or ""),
            actor=principal,
        )
        result = await run_approval_flow(
            config,
            {
                "op": "resolve",
                "approval_id": record["request_id"],
                "decision": decision,
                "reason": reason,
            },
            "/tmp/veya-approvals",
        )
        if result.get("status") != "completed":
            raise HTTPException(
                status_code=409,
                detail=str(result.get("error") or "approval not pending"),
            )
        wake_pending_approval(record["request_id"])

        # W17: record approved decision in ask-once cache
        if decision in {"allow", "once", "session", "always"}:
            from server.ask_once_cache import get_ask_once_cache

            sid = str(record.get("session_id") or "")
            if sid:
                get_ask_once_cache().record(
                    session_id=sid,
                    tool_or_action=str(record.get("tool") or ""),
                    arguments=record.get("tool_args") or {},
                    decision=decision,
                    approval_id=str(record.get("request_id") or approval_id),
                )

        return result

    # ── human handoff ─────────────────────────────────────────────────
    async def request_handoff(
        self, *, reason: str, user_id: str, session_id: str = ""
    ) -> dict[str, Any]:
        """Open a paused 'user must do this step' handoff request."""

        from veya.omodul.approval_flow import ApprovalFlowConfig, run_approval_flow

        scope = self.scope_for(user_id)
        config = ApprovalFlowConfig(
            store=self.store,
            trail=lambda topic, payload: _trail(topic, payload, actor="master"),
            scope=scope,
            session_id=session_id,
        )
        result = await run_approval_flow(
            config,
            {"op": "handoff_request", "reason": reason},
            "/tmp/veya-approvals",
        )
        if result.get("status") != "completed":
            raise RuntimeError(str(result.get("error") or "handoff request failed"))
        return result

    def get_handoff(self, handoff_id: str) -> dict[str, Any] | None:
        return self.store.get_handoff(str(handoff_id))

    async def resume_handoff(
        self, *, handoff_id: str, done: bool, principal: str = "human"
    ) -> dict[str, Any]:
        from veya.omodul.approval_flow import ApprovalFlowConfig, run_approval_flow

        record = self.store.get_handoff(str(handoff_id))
        if record is None:
            raise HTTPException(status_code=404, detail="handoff not found")
        if record.get("status") != "pending":
            raise HTTPException(status_code=409, detail="handoff already resolved")
        config = ApprovalFlowConfig(
            store=self.store,
            trail=lambda topic, payload: _trail(topic, payload, actor=principal),
            scope=str(record.get("scope") or ""),
        )
        result = await run_approval_flow(
            config,
            {"op": "handoff_done" if done else "handoff_cancel", "handoff_id": record["id"]},
            "/tmp/veya-approvals",
        )
        if result.get("status") != "completed":
            raise HTTPException(
                status_code=409,
                detail=str(result.get("error") or "handoff not pending"),
            )
        wake_pending_handoff(record["id"])
        return result


_service: ApprovalService | None = None


def get_approval_service() -> ApprovalService:
    global _service
    if _service is None:
        _service = ApprovalService()
    return _service


def configure_service(*, store: ApprovalStore | None = None) -> ApprovalService:
    """Bind the service to a specific store (tests, isolated deployments)."""

    global _service
    _service = ApprovalService(store=store)
    return _service


# ── REST ───────────────────────────────────────────────────────────────


class ApprovalResolveRequest(BaseModel):
    decision: str = Field(pattern="^(once|session|always|deny)$")
    reason: str = ""
    # Resolver identity is deliberately not a request field. The endpoint
    # derives a pseudonymous human principal from the authenticated user.


class HandoffCreateRequest(BaseModel):
    reason: str = Field(..., min_length=1, max_length=2000)


def _scope_of(user: dict[str, Any]) -> str:
    return get_approval_service().scope_for(str(user.get("user_id") or "anonymous"))


@router.get("/approvals")
async def list_approvals(
    status: str = "pending",
    user: dict = Depends(auth_mod.require_user),
) -> dict[str, Any]:
    """Pending approvals for the signed-in user (pseudonymized scope)."""

    service = get_approval_service()
    rows = service.store.list_approvals(scope=_scope_of(user), status=status)
    return {"approvals": rows, "scope": _scope_of(user)}


@router.post("/approvals/{approval_id}/resolve")
async def resolve_approval_endpoint(
    approval_id: str,
    body: ApprovalResolveRequest,
    user: dict = Depends(auth_mod.require_user),
) -> dict[str, Any]:
    """Resolve a pending approval (guard L: human unless delegated)."""

    # an approval can only be answered from the account it belongs to
    service = get_approval_service()
    current = service.get(approval_id)
    if not current or str(current.get("scope") or "") != _scope_of(user):
        raise HTTPException(status_code=404, detail="approval not found")
    # The human resolver is derived from the authenticated account's existing
    # approval scope. Body fields and X-Veya-Principal headers are never trusted.
    principal = f"human:{_scope_of(user)}"
    result = await service.resolve(
        approval_id=approval_id,
        decision=body.decision,
        reason=body.reason,
        principal=principal,
    )
    return result


@router.get("/grants")
async def list_grants(
    user: dict = Depends(auth_mod.require_user),
) -> dict[str, Any]:
    """The signed-in user's 'always allow' grants."""

    service = get_approval_service()
    return {"grants": service.list_grants(scope=_scope_of(user))}


@router.delete("/grants/{grant_id}")
async def revoke_grant_endpoint(
    grant_id: int,
    user: dict = Depends(auth_mod.require_user),
) -> dict[str, Any]:
    """Revoke one 'always allow' grant."""

    if not get_approval_service().revoke_grant(grant_id, scope=_scope_of(user)):
        raise HTTPException(status_code=404, detail="grant not found")
    return {"revoked": grant_id}


@router.post("/handoff")
async def create_handoff(
    body: HandoffCreateRequest,
    user: dict = Depends(auth_mod.require_user),
) -> dict[str, Any]:
    """Open a human-handoff request (agent side: 'user must do this step')."""

    result = await get_approval_service().request_handoff(
        reason=body.reason,
        user_id=str(user.get("user_id") or "anonymous"),
    )
    return result


def _own_handoff_or_404(handoff_id: str, user: dict[str, Any]) -> None:
    """A handoff can only be completed/cancelled from the account it belongs to."""

    current = get_approval_service().get_handoff(handoff_id)
    if not current or str(current.get("scope") or "") != _scope_of(user):
        raise HTTPException(status_code=404, detail="handoff not found")


@router.post("/handoff/{handoff_id}/done")
async def handoff_done(
    handoff_id: str,
    user: dict = Depends(auth_mod.require_user),
) -> dict[str, Any]:
    _own_handoff_or_404(handoff_id, user)
    return await get_approval_service().resume_handoff(handoff_id=handoff_id, done=True)


@router.post("/handoff/{handoff_id}/cancel")
async def handoff_cancel(
    handoff_id: str,
    user: dict = Depends(auth_mod.require_user),
) -> dict[str, Any]:
    _own_handoff_or_404(handoff_id, user)
    return await get_approval_service().resume_handoff(handoff_id=handoff_id, done=False)


__all__ = [
    "FLAG_APPROVAL_GATES",
    "ApprovalService",
    "approval_gates_enabled",
    "get_approval_service",
    "router",
    "session_grants",
    "wake_pending_approval",
    "wake_pending_handoff",
]
