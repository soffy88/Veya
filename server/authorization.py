"""Single fail-closed authorization boundary for durable Veya objects.

This module is deliberately policy-only.  It does not execute tools, create
GoalRuns, persist state, or decide acceptance.  Callers pass explicit identity
and resource metadata; missing metadata is denied rather than inferred.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


class AuthorizationDenied(PermissionError):
    """An access request failed the explicit authorization contract."""


@dataclass(frozen=True)
class Principal:
    subject_id: str
    tenant_id: str
    bot_id: str | None = None
    scopes: frozenset[str] = frozenset()


@dataclass(frozen=True)
class Resource:
    kind: str
    owner_id: str
    tenant_id: str
    bot_id: str | None = None


@dataclass(frozen=True)
class Delegation:
    source_bot_id: str
    target_bot_id: str
    delegate_id: str
    allowed_scopes: frozenset[str] = frozenset()


def authorize(
    principal: Principal,
    action: str,
    resource: Resource,
    *,
    delegation: Delegation | None = None,
) -> None:
    """Authorize one access, raising for every implicit or cross-owner path."""
    if not principal.subject_id or not principal.tenant_id:
        raise AuthorizationDenied("principal identity is required")
    if not action or not resource.kind or not resource.owner_id or not resource.tenant_id:
        raise AuthorizationDenied("resource identity and action are required")
    if principal.tenant_id != resource.tenant_id:
        raise AuthorizationDenied("cross-tenant access denied")

    if delegation is not None:
        if principal.bot_id != delegation.target_bot_id:
            raise AuthorizationDenied("delegated principal does not match target bot")
        if action not in delegation.allowed_scopes:
            raise AuthorizationDenied("delegated action is outside the explicit grant")
        if resource.bot_id != delegation.target_bot_id:
            raise AuthorizationDenied("delegation cannot borrow another bot resource")
    elif resource.bot_id is not None and principal.bot_id != resource.bot_id:
        raise AuthorizationDenied("cross-bot access denied")

    if principal.subject_id != resource.owner_id and action not in principal.scopes:
        raise AuthorizationDenied("owner or explicit scope is required")


def authorize_tool(
    principal: Principal,
    tool: str,
    *,
    physical: bool,
    gateway_bound: bool,
) -> None:
    """Apply default-deny tool policy before any physical callable is reached."""
    if not principal.subject_id or not principal.tenant_id or not tool:
        raise AuthorizationDenied("explicit tool principal is required")
    if physical and not gateway_bound:
        raise AuthorizationDenied("physical tools require ActionGateway binding")
    if tool not in principal.scopes and "tool:*" not in principal.scopes:
        raise AuthorizationDenied(f"tool {tool!r} is not authorized")


def redact(value: Any) -> Any:
    """Return a recursively redacted JSON-safe projection for untrusted sinks."""
    secret_names = {"secret", "token", "password", "passwd", "api_key", "authorization"}
    if isinstance(value, dict):
        return {
            str(key): "[REDACTED]" if str(key).lower() in secret_names else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact(item) for item in value]
    return value


__all__ = [
    "AuthorizationDenied",
    "Delegation",
    "Principal",
    "Resource",
    "authorize",
    "authorize_tool",
    "redact",
]
