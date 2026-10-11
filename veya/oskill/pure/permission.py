"""3O-PURE — evaluate_capability_permission: tool+args+grants -> allow/ask/deny.

Stateless permission evaluation for the NANO-W6 approval gates. No I/O, no
user identity (the caller passes a pseudonymized scope + already-fetched
grants), no clocks. High-risk classes (DB migrations, payment/secret access,
production) are human-only: they always evaluate to ``ask``.
"""

from __future__ import annotations

from typing import Any

#: Human-only classes: always require a human approval, delegation or not.
HUMAN_ONLY_MARKERS = frozenset(
    {
        "db_migration",
        "payment",
        "secret_access",
        "production",
        "migrate",
        "database_migration",
    }
)

#: Gated classes: destructive / outbound / payment-like tool calls.
GATED_MARKERS = frozenset(
    {
        "delete",
        "remove",
        "destroy",
        "drop",
        "send",
        "push",
        "publish",
        "post",
        "email",
        "message",
        "spend",
        "pay",
        "purchase",
        "deploy",
        "upload",
        "write",
        "edit",
        "patch",
        "create",
    }
)

VERDICT_ALLOW = "allow"
VERDICT_ASK = "ask"
VERDICT_DENY = "deny"


def scope_match(pattern: str, scope: str) -> bool:
    """Whether a grant's scope pattern covers a scope. Empty pattern = all."""

    pat = str(pattern or "").strip()
    if not pat:
        return True
    return pat == scope or scope.startswith(pat)


def _matches(tool: str, markers: frozenset[str]) -> bool:
    name = str(tool or "").strip().lower()
    return any(marker in name for marker in markers)


def evaluate_capability_permission(
    *,
    tool: str,
    args: dict[str, Any] | None = None,
    grants: list[dict[str, Any]] | None = None,
    flags: dict[str, Any] | None = None,
) -> str:
    """→ "allow" | "ask" | "deny".

    allow — an always-grant covers this tool+scope;
    deny  — an explicit policy flag refuses ({"deny": [tool, ...]});
    ask   — gated or human-only classes, or anything unrecognized
            (fail closed: a requirement nobody evaluated is not permission).
    """

    del args  # args are shown to the approver; the verdict is class-based
    name = str(tool or "").strip().lower()
    if _matches(name, HUMAN_ONLY_MARKERS):
        return VERDICT_ASK
    deny_list = (flags or {}).get("deny") or []
    if name in {str(item).lower() for item in deny_list}:
        return VERDICT_DENY
    scope = str((flags or {}).get("scope") or "")
    for grant in grants or []:
        if str(grant.get("tool") or "").lower() != name:
            continue
        if scope_match(str(grant.get("scope_pattern") or ""), scope):
            return VERDICT_ALLOW
    if _matches(name, GATED_MARKERS):
        return VERDICT_ASK
    return VERDICT_ASK


__all__ = [
    "evaluate_capability_permission",
    "scope_match",
    "HUMAN_ONLY_MARKERS",
    "GATED_MARKERS",
    "VERDICT_ALLOW",
    "VERDICT_ASK",
    "VERDICT_DENY",
]
