"""Server-only tenant identity for Veya.

Design adapted from block/buzz (Apache-2.0, Copyright 2026 Block, Inc.):
community/tenant identity is resolved on the server from the authenticated
principal and the request Host, never from a client-supplied payload field.
This is a Veya implementation; no Buzz Rust code is copied.

``TenantContext`` is frozen and requires a private server token, so a request
body cannot be unpacked into one: the only supported construction path is
:func:`resolve_tenant_context`, which takes trusted ASGI scope fields and an
already-authenticated user.  A contextvar carries the result through the
request and into any task spawned from it (runtime/execution included).

Flag: ``VEYA_TENANT_STRICT=1`` fails closed (HTTP 403 from the auth gate) when
the Host and user cannot be mapped.  Default ``0`` keeps single-tenant
deployments on ``default`` for unmapped hosts, preserving current behaviour.
"""

from __future__ import annotations

import contextvars
import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

__all__ = [
    "DEFAULT_TENANT",
    "TenantContext",
    "TenantResolutionError",
    "bind_tenant_context",
    "current_tenant",
    "reset_tenant_context",
    "resolve_tenant_context",
]

DEFAULT_TENANT: Final = "default"


class TenantResolutionError(ValueError):
    """Raised when a strict-mode tenant cannot be resolved safely."""


class _ServerToken:
    """Opaque capability: only this module constructs it."""

    __slots__ = ()


_SERVER_TOKEN: Final = _ServerToken()


@dataclass(frozen=True, slots=True)
class TenantContext:
    """Immutable tenant identity; client payloads cannot create one."""

    tenant_id: str
    _server_token: _ServerToken
    source: str = "default"

    def __post_init__(self) -> None:
        if self._server_token is not _SERVER_TOKEN:
            raise TenantResolutionError("TenantContext is server-only")
        tid = str(self.tenant_id)
        if not tid or len(tid) > 128 or any(c.isspace() for c in tid):
            raise TenantResolutionError("invalid tenant id")


_DEFAULT_CONTEXT: Final = TenantContext(DEFAULT_TENANT, _SERVER_TOKEN)


_tenant_ctx: contextvars.ContextVar[TenantContext] = contextvars.ContextVar(
    "veya_tenant", default=_DEFAULT_CONTEXT
)


def current_tenant() -> TenantContext:
    """Tenant of the current request/task; ``default`` outside a request."""

    return _tenant_ctx.get()


def bind_tenant_context(ctx: TenantContext) -> contextvars.Token[TenantContext]:
    return _tenant_ctx.set(ctx)


def reset_tenant_context(token: contextvars.Token[TenantContext]) -> None:
    _tenant_ctx.reset(token)


def _host_from_scope(scope: Mapping[str, Any]) -> str:
    """Host header from ASGI scope only; never trust a JSON body field."""

    for key, value in scope.get("headers") or ():
        if key.lower() == b"host":
            return str(value.decode("latin-1").split(":", 1)[0].strip().lower())
    server = scope.get("server") or ()
    return str(server[0]).strip().lower() if server else ""


def resolve_tenant_context(
    scope: Mapping[str, Any],
    user: Mapping[str, Any] | None,
    *,
    env: Mapping[str, str] | None = None,
) -> TenantContext:
    """Resolve tenant from authenticated user + Host (server inputs only).

    Host mapping takes precedence; user mapping is used when the Host is not
    mapped.  A conflict between the two fails closed in strict mode.  Unknown
    inputs resolve to ``default`` in non-strict mode.
    """

    # ``env`` is only for tests; production always reads process environment.
    source_env: Mapping[str, str] = os.environ if env is None else env

    def flag_strict() -> bool:
        return str(source_env.get("VEYA_TENANT_STRICT", "0")).strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }

    def mapping(name: str) -> dict[str, str]:
        result: dict[str, str] = {}
        for piece in str(source_env.get(name, "")).split(","):
            if "=" not in piece:
                continue
            key, value = (part.strip().lower() for part in piece.split("=", 1))
            if key and value:
                result[key] = value
        return result

    host = _host_from_scope(scope)
    host_map = mapping("VEYA_TENANT_HOST_MAP")
    user_map = mapping("VEYA_TENANT_USER_MAP")
    user_id = str((user or {}).get("user_id") or "").strip().lower()
    host_tenant = host_map.get(host, "")
    user_tenant = user_map.get(user_id, "")
    if host_tenant and user_tenant and host_tenant != user_tenant and flag_strict():
        raise TenantResolutionError("tenant mapping conflict")
    if host_tenant:
        return TenantContext(host_tenant, _SERVER_TOKEN, source="host")
    if user_tenant:
        return TenantContext(user_tenant, _SERVER_TOKEN, source="user")
    if flag_strict():
        raise TenantResolutionError("unmapped host/user in strict mode")
    return TenantContext(DEFAULT_TENANT, _SERVER_TOKEN)
