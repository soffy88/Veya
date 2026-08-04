"""obase.authz — permission evaluation and interactive confirmation (3O §7, an obase
cross-cutting concern).

G5: upgrades "rule-based callable always allows" into a real decision state machine:
    ALLOW (rule explicitly allows) → DENY (rule explicitly denies) → PENDING
    (ask: rule / no match → human confirmation).

This module is the **canonical single source** of permission rules (§1.4):
    - ``veya.compat.permission_evaluate`` / ``match_permission_rule`` delegate here;
    - the project service layer (CLI/HTTP) drives confirmation via
      ``InteractivePermissionGate``.

Rule syntax (consistent with the existing ``_RULES_BY_PERSONA``):
    ``allow:<action>`` / ``deny:<action>`` / ``ask:<action>`` / ``allow:*`` wildcard.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import time
import uuid
from collections.abc import Callable
from enum import StrEnum
from typing import Any

__all__ = [
    "InteractivePermissionGate",
    "PermissionDecision",
    "PermissionRequest",
    "evaluate_permission",
    "match_permission_rule",
]


class PermissionDecision(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    PENDING = "pending"


@dataclasses.dataclass
class PermissionRequest:
    """A single permission request (pending or decided)."""

    request_id: str
    action: str
    resource: str | None
    persona: str
    context: dict[str, Any]
    decision: PermissionDecision = PermissionDecision.PENDING
    decided_at: float | None = None
    note: str = ""

    def decide(self, decision: PermissionDecision, *, note: str = "") -> None:
        self.decision = decision
        self.note = note
        self.decided_at = time.time()


# ── 规则匹配（canonical） ─────────────────────────────────────────────
def match_permission_rule(rules: list[str], action: str, resource: str | None = None) -> str | None:
    """Match ``allow:``/``deny:``/``ask:`` rules in order; return the matched verb or None.

    A wildcard ``allow:*`` matches any action. Rule order wins (first match takes effect).
    """
    for rule in rules:
        rule = rule.strip()
        if ":" not in rule:
            continue
        verb, target = rule.split(":", 1)
        verb = verb.lower()
        if target == "*" or target == action or (resource and target == resource):
            return verb
    return None


def evaluate_permission(
    action: str,
    *,
    resource: str | None = None,
    persona: str = "build",
    rules: list[str] | None = None,
) -> dict[str, Any]:
    """Evaluate a permission → decision dict (ALLOW/DENY/PENDING three states).

    Return shape (omodul-style status/error fields, §5.3):
        {"decision": "allow|deny|pending", "action": ..., "resource": ...,
         "persona": ..., "matched_rule": ...|None, "status": "decided"|"pending",
         "error": None}
    """
    if rules is None:
        rules = _default_rules(persona)
    verb = match_permission_rule(rules, action, resource)
    if verb == "allow":
        decision, status = PermissionDecision.ALLOW, "decided"
    elif verb == "deny":
        decision, status = PermissionDecision.DENY, "decided"
    elif verb == "ask":
        decision, status = PermissionDecision.PENDING, "pending"
    else:
        # 无匹配：安全默认 = 待确认（比静默 allow 更符合 security-by-default）
        decision, status = PermissionDecision.PENDING, "pending"
    return {
        "decision": decision,
        "action": action,
        "resource": resource,
        "persona": persona,
        "matched_rule": verb,
        "status": status,
        "error": None,
    }


def _default_rules(persona: str) -> list[str]:
    """Per-persona default rules (aligned with config/permissions.py _RULES_BY_PERSONA)."""
    if persona == "build":
        # ask 优先于 allow:* —— 否则 allow:* 会吞掉 ask:bash（规则顺序先匹配先生效）
        return ["ask:bash", "allow:*"]
    if persona in ("plan", "research"):
        return ["deny:write", "deny:edit", "deny:bash", "allow:*"]
    return ["allow:*"]


# ── 交互式确认门（G5） ────────────────────────────────────────────────
class InteractivePermissionGate:
    """Suspend PENDING requests into approve/deny-able requests with sync/async waiting.

    - Rules yielding ALLOW/DENY return immediately without disturbing the user.
    - Rules yielding PENDING (ask: or no match) create a suspended ``PermissionRequest``,
      notified via the ``on_pending`` callback (CLI → input(); HTTP → SSE/polling);
      the caller blocks on ``await_decision`` until approve/deny or timeout.
    """

    def __init__(
        self,
        *,
        on_pending: Callable[[PermissionRequest], Any] | None = None,
        auto_approve_timeout: float = 60.0,
        default_timeout: float = 60.0,
    ) -> None:
        self._pending: dict[str, PermissionRequest] = {}
        self._events: dict[str, asyncio.Event] = {}
        self._resolved: dict[str, PermissionRequest] = {}  # 最近已决（响应可查询）
        self._on_pending = on_pending
        self._auto_approve_timeout = auto_approve_timeout
        self._default_timeout = default_timeout

    # ── 主入口 ──────────────────────────────────────────────────────
    async def evaluate(
        self,
        action: str,
        *,
        resource: str | None = None,
        persona: str = "build",
        context: dict[str, Any] | None = None,
        rules: list[str] | None = None,
        wait: bool = True,
    ) -> dict[str, Any]:
        """Evaluate and interactively confirm when needed. With ``wait=False`` a PENDING decision returns directly (suspended)."""
        result = evaluate_permission(action, resource=resource, persona=persona, rules=rules)
        if result["decision"] != PermissionDecision.PENDING:
            return result
        request = self._enqueue(action, resource, persona, context or {}, rules)
        if not wait:
            result["request_id"] = request.request_id
            return result
        return await self._resolve(request)

    # ── 队列管理 ────────────────────────────────────────────────────
    def _enqueue(
        self,
        action: str,
        resource: str | None,
        persona: str,
        context: dict[str, Any],
        rules: list[str] | None,
    ) -> PermissionRequest:
        request = PermissionRequest(
            request_id=uuid.uuid4().hex[:12],
            action=action,
            resource=resource,
            persona=persona,
            context=context,
        )
        self._pending[request.request_id] = request
        self._events[request.request_id] = asyncio.Event()
        if self._on_pending is not None:
            with contextlib.suppress(Exception):
                self._on_pending(request)  # 通知失败不阻塞
        return request

    def pending_requests(self) -> list[PermissionRequest]:
        return list(self._pending.values())

    def get_request(self, request_id: str) -> PermissionRequest | None:
        return self._pending.get(request_id) or self._resolved.get(request_id)

    def approve(self, request_id: str, *, note: str = "approved by user") -> bool:
        """Human approval (synchronous; for CLI/HTTP callbacks)."""
        request = self._pending.get(request_id)
        if request is None or request.decision != PermissionDecision.PENDING:
            return False
        request.decide(PermissionDecision.ALLOW, note=note)
        self._resolve_event(request_id)
        return True

    def deny(self, request_id: str, *, note: str = "denied by user") -> bool:
        request = self._pending.get(request_id)
        if request is None or request.decision != PermissionDecision.PENDING:
            return False
        request.decide(PermissionDecision.DENY, note=note)
        self._resolve_event(request_id)
        return True

    def reject_stale(self, *, max_age_seconds: float | None = None) -> int:
        """Auto-DENY stale pending requests (safe default). Returns the number processed."""
        stale = [
            r
            for r in self._pending.values()
            if r.decision == PermissionDecision.PENDING and r.decided_at is None
        ]
        for request in stale:
            self.deny(request.request_id, note="auto-denied: stale request")
        return len(stale)

    # ── 等待 ────────────────────────────────────────────────────────
    async def await_decision(
        self, request_id: str, *, timeout: float | None = None
    ) -> dict[str, Any]:
        """Block until approve/deny or timeout (timeout → DENY, the safe default)."""
        request = self._pending.get(request_id)
        if request is None:
            return {"decision": PermissionDecision.DENY, "error": "unknown request_id"}
        event = self._events[request_id]
        try:
            await asyncio.wait_for(event.wait(), timeout or self._default_timeout)
        except TimeoutError:
            self.deny(request_id, note="timeout auto-denied")
        return self._as_result(request)

    async def _resolve(self, request: PermissionRequest) -> dict[str, Any]:
        result = await self.await_decision(request.request_id, timeout=self._default_timeout)
        return result

    def _resolve_event(self, request_id: str) -> None:
        event = self._events.get(request_id)
        if event is not None:
            event.set()
        # 决出后从挂起队列移入已决记录（保留最近 100 条供响应查询）
        request = self._pending.pop(request_id, None)
        self._events.pop(request_id, None)
        if request is not None:
            self._resolved[request_id] = request
            if len(self._resolved) > 100:
                oldest = next(iter(self._resolved))
                del self._resolved[oldest]

    def _as_result(self, request: PermissionRequest) -> dict[str, Any]:
        return {
            "decision": request.decision,
            "action": request.action,
            "resource": request.resource,
            "persona": request.persona,
            "request_id": request.request_id,
            "note": request.note,
            "status": "decided",
            "error": None,
        }
