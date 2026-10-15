"""veya/oprim/approval — approval/grant/handoff atomic ops (NANO-W6).

One atomic op per function, all persistence through an injected
:class:`~veya.obase.approvals.ApprovalStore` handle (default: the shared store
at the conventional path). No oprim→oprim calls, no decision_trail — the
trail is written by the omodul flow above.
"""

from __future__ import annotations

from typing import Any


def _store_of(store: Any) -> Any:
    if store is not None:
        return store
    from veya.obase.approvals import ApprovalStore

    return ApprovalStore()


def approval_create(record: dict[str, Any], *, store: Any = None) -> dict[str, Any]:
    """Insert one pending approval (idempotent by request_id). Returns the row."""

    return _store_of(store).create_approval(record)


def approval_resolve(
    request_id: str,
    *,
    decision: str,
    reason: str = "",
    store: Any = None,
) -> dict[str, Any] | None:
    """Resolve one pending approval exactly once (CAS). None if not pending."""

    return _store_of(store).resolve_approval(request_id, decision=decision, reason=reason)


def grant_lookup(
    *,
    scope: str,
    tool: str,
    scope_pattern: str = "",
    store: Any = None,
) -> dict[str, Any] | None:
    """One always-grant for (scope, tool, scope_pattern), or None."""

    return _store_of(store).lookup_grant(scope=scope, tool=tool, scope_pattern=scope_pattern)


def grant_scope_match(pattern: str, scope: str, *, store: Any = None) -> bool:
    """Whether a grant's scope_pattern covers scope. Empty pattern = all."""

    del store  # pure predicate; the handle keeps the op injectable
    pat = str(pattern or "").strip()
    if not pat:
        return True
    return pat == scope or scope.startswith(pat)


def grant_put(
    *,
    scope: str,
    tool: str,
    scope_pattern: str = "",
    store: Any = None,
) -> dict[str, Any]:
    """Insert or refresh one always-grant. Returns the row."""

    return _store_of(store).put_grant(scope=scope, tool=tool, scope_pattern=scope_pattern)


def grant_revoke(grant_id: int, *, store: Any = None) -> bool:
    """Delete one always-grant. True when a row was removed."""

    return _store_of(store).revoke_grant(grant_id)


def grant_list(*, scope: str = "", store: Any = None) -> list[dict[str, Any]]:
    """List always-grants, optionally filtered by scope."""

    return _store_of(store).list_grants(scope=scope)


def handoff_create(record: dict[str, Any], *, store: Any = None) -> dict[str, Any]:
    """Insert one pending human-handoff request (idempotent by id)."""

    return _store_of(store).create_handoff(record)


def handoff_resolve(
    handoff_id: str,
    *,
    status: str,
    store: Any = None,
) -> dict[str, Any] | None:
    """Resolve one pending handoff (done/cancelled) exactly once (CAS)."""

    return _store_of(store).resolve_handoff(handoff_id, status=status)


__all__ = [
    "approval_create",
    "approval_resolve",
    "grant_list",
    "grant_lookup",
    "grant_put",
    "grant_revoke",
    "grant_scope_match",
    "handoff_create",
    "handoff_resolve",
]
