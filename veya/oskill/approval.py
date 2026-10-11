"""veya/oskill/approval — composite permission decision (NANO-W6).

Composes the oprim atoms (grant_lookup + grant_scope_match) with the pure
evaluate_capability_permission into one decision: allow / ask / deny. No I/O
of its own — all persistence flows through the injected store handle on the
ops; no user_id (the caller passes a pseudonymized scope).
"""

from __future__ import annotations

from typing import Any

from veya.oprim.approval import grant_lookup, grant_scope_match
from veya.oskill.pure.permission import (
    VERDICT_ALLOW,
    VERDICT_ASK,
    VERDICT_DENY,
    evaluate_capability_permission,
    scope_match,
)


def request_permission_decision(
    *,
    tool: str,
    args: dict[str, Any] | None = None,
    scope: str,
    flags: dict[str, Any] | None = None,
    store: Any = None,
    extra_grants: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Decide whether a tool call may run, must ask, or is denied.

    → {"decision": "allow"|"ask"|"deny", "grant": <row|None>, "reason": str}
    ``extra_grants`` carries in-memory session grants alongside the durable
    always-grants fetched from the store.
    """

    grant = grant_lookup(scope=scope, tool=tool, store=store)
    covered = bool(grant) and grant_scope_match(
        str(grant.get("scope_pattern") or ""), scope
    )
    grants = [grant] if covered else []
    for extra in extra_grants or []:
        if str(extra.get("tool") or "").lower() != str(tool or "").lower():
            continue
        if scope_match(str(extra.get("scope_pattern") or ""), scope):
            grants.append(extra)
    verdict = evaluate_capability_permission(
        tool=tool, args=args, grants=grants, flags={**(flags or {}), "scope": scope}
    )
    if verdict == VERDICT_ALLOW and grants:
        return {
            "decision": "allow",
            "grant": grant if covered else grants[0],
            "reason": (
                "always-allow grant covers this tool+scope"
                if covered
                else "session grant covers this tool+scope"
            ),
        }
    if verdict == VERDICT_DENY:
        return {"decision": "deny", "grant": None, "reason": "policy denies this tool"}
    return {
        "decision": "ask",
        "grant": grant if covered else None,
        "reason": "no always-allow grant covers this tool+scope; human approval required",
    }


__all__ = ["request_permission_decision"]
