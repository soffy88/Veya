"""veya/omodul/approval_flow — approval request/resolve/handoff flow (NANO-W6).

omodul contract: ``run_approval_flow(config, input_data, output_dir) -> dict``
with ``status``/``error`` keys; never raises. ``_enabled_pillars`` declares
``decision_trail`` — every security/approval-relevant step appends a trail
record through the injected ``trail`` callable (the service layer binds it to
the canonical event store; no PII — the caller passes a pseudonymized scope).

Ops (``input_data["op"]``):
  * ``request``  — create a pending approval, fire the push relay, trail it.
  * ``resolve``  — apply a decision (once/session/always/deny), trail it.
  * ``handoff_request`` — create a paused human-handoff request, trail it.
  * ``handoff_done`` / ``handoff_cancel`` — resume the paused run, trail it.
"""

from __future__ import annotations

import contextlib
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from veya.oprim.approval import (
    approval_create,
    approval_resolve,
    grant_put,
    handoff_create,
    handoff_resolve,
)

STATUS_COMPLETED = "completed"
STATUS_ERROR = "error"


class ApprovalFlowConfig:
    """Injection points for the approval flow."""

    _enabled_pillars: frozenset[str] = frozenset({"decision_trail"})

    def __init__(
        self,
        *,
        store: Any = None,
        notify: Callable[[str, dict[str, Any]], None] | None = None,
        trail: Callable[[str, dict[str, Any]], None] | None = None,
        scope: str = "",
        session_id: str = "",
        actor: str = "",
    ) -> None:
        self.store = store
        self.notify = notify
        self.trail = trail
        self.scope = scope
        self.session_id = session_id
        self.actor = actor


def _trail(config: ApprovalFlowConfig, topic: str, payload: dict[str, Any]) -> None:
    if config.trail is None:
        return
    with contextlib.suppress(Exception):  # a trail failure must never block an approval decision
        config.trail(topic, payload)


async def run_approval_flow(
    config: ApprovalFlowConfig,
    input_data: Any,
    output_dir: Path = Path("/tmp/veya"),
) -> dict[str, Any]:
    """Run one approval-flow op. Never raises; errors return status=error."""

    try:
        data = dict(input_data or {})
        op = str(data.get("op") or "")
        if op == "request":
            return _op_request(config, data)
        if op == "resolve":
            return _op_resolve(config, data)
        if op == "handoff_request":
            return _op_handoff_request(config, data)
        if op in {"handoff_done", "handoff_cancel"}:
            return _op_handoff_resume(config, data, op)
        return {"status": STATUS_ERROR, "error": f"unknown op: {op}"}
    except Exception as exc:  # omodul contract: never raises
        return {"status": STATUS_ERROR, "error": f"{type(exc).__name__}: {exc}"}


def _op_request(config: ApprovalFlowConfig, data: dict[str, Any]) -> dict[str, Any]:
    request_id = str(data.get("request_id") or uuid.uuid4().hex[:12])
    record = approval_create(
        {
            "request_id": request_id,
            "scope": config.scope,
            "tool": str(data.get("tool") or ""),
            "tool_args": data.get("tool_args") or {},
            "delegate_policy": str(data.get("delegate_policy") or ""),
            "session_id": config.session_id,
            "actor": config.actor,
            "created_at": time.time(),
        },
        store=config.store,
    )
    _trail(
        config,
        "approval.requested",
        {
            "request_id": request_id,
            "tool": record["tool"],
            "scope": config.scope,
            "session_id": config.session_id,
        },
    )
    if config.notify is not None:
        with contextlib.suppress(Exception):  # a push failure must not lose the approval record
            config.notify(
                {
                    "type": "APPROVAL_REQUIRED",
                    "title": f"Approval required: {record['tool']}",
                    "content": str(data.get("reason") or record["tool"]),
                    "payload": {
                        "approval_id": request_id,
                        "deep_link": f"/approvals/{request_id}",
                        "tool": record["tool"],
                    },
                }
            )
    return {
        "status": STATUS_COMPLETED,
        "op": "request",
        "approval_id": request_id,
        "approval": record,
    }


def _op_resolve(config: ApprovalFlowConfig, data: dict[str, Any]) -> dict[str, Any]:
    request_id = str(data.get("approval_id") or "")
    decision = str(data.get("decision") or "once")
    reason = str(data.get("reason") or "")
    row = approval_resolve(request_id, decision=decision, reason=reason, store=config.store)
    if row is None:
        return {
            "status": STATUS_ERROR,
            "error": "approval not pending or unknown",
            "approval_id": request_id,
        }
    if decision == "always":
        grant_put(
            scope=row["scope"],
            tool=row["tool"],
            scope_pattern=str(data.get("scope_pattern") or ""),
            store=config.store,
        )
    _trail(
        config,
        "approval.resolved",
        {
            "request_id": request_id,
            "tool": row["tool"],
            "decision": decision,
            "scope": row["scope"],
            "reason": reason,
        },
    )
    return {
        "status": STATUS_COMPLETED,
        "op": "resolve",
        "approval_id": request_id,
        "decision": decision,
        "approval": row,
    }


def _op_handoff_request(config: ApprovalFlowConfig, data: dict[str, Any]) -> dict[str, Any]:
    handoff_id = str(data.get("handoff_id") or uuid.uuid4().hex[:12])
    record = handoff_create(
        {
            "id": handoff_id,
            "scope": config.scope,
            "reason": str(data.get("reason") or "user must do this step"),
            "created_at": time.time(),
        },
        store=config.store,
    )
    _trail(
        config,
        "handoff.requested",
        {"handoff_id": handoff_id, "scope": config.scope, "reason": record["reason"]},
    )
    return {
        "status": STATUS_COMPLETED,
        "op": "handoff_request",
        "handoff_id": handoff_id,
        "handoff": record,
    }


def _op_handoff_resume(config: ApprovalFlowConfig, data: dict[str, Any], op: str) -> dict[str, Any]:
    handoff_id = str(data.get("handoff_id") or "")
    status = "done" if op == "handoff_done" else "cancelled"
    row = handoff_resolve(handoff_id, status=status, store=config.store)
    if row is None:
        return {
            "status": STATUS_ERROR,
            "error": "handoff not pending or unknown",
            "handoff_id": handoff_id,
        }
    _trail(
        config,
        "handoff.resumed",
        {"handoff_id": handoff_id, "status": status, "scope": row["scope"]},
    )
    return {
        "status": STATUS_COMPLETED,
        "op": op,
        "handoff_id": handoff_id,
        "handoff": row,
    }


__all__ = ["STATUS_COMPLETED", "STATUS_ERROR", "ApprovalFlowConfig", "run_approval_flow"]
