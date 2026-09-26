"""Error normalization for the pinned cfKanban HTTP contract."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class CfKanbanErrorClass(StrEnum):
    AUTH_REQUIRED = "AUTH_REQUIRED"
    FORBIDDEN = "FORBIDDEN"
    NOT_FOUND = "NOT_FOUND"
    CONFLICT = "CONFLICT"
    RATE_LIMITED = "RATE_LIMITED"
    VALIDATION_ERROR = "VALIDATION_ERROR"
    TRANSIENT = "TRANSIENT"
    UPSTREAM_UNAVAILABLE = "UPSTREAM_UNAVAILABLE"
    AMBIGUOUS = "AMBIGUOUS"
    QUOTA_EXCEEDED = "QUOTA_EXCEEDED"
    INVALID_STATE = "INVALID_STATE"
    UNKNOWN = "UNKNOWN"


@dataclass
class CfKanbanProviderError(RuntimeError):
    """Normalized error retaining safe provider diagnostics."""

    canonical_class: CfKanbanErrorClass
    message: str
    provider_code: str | None = None
    provider_http_status: int | None = None
    provider_request_id: str | None = None
    recovery_hint: str | None = None
    retryable: bool = False
    details: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        RuntimeError.__init__(self, self.message)

    def to_dict(self) -> dict[str, Any]:
        return {
            "canonical_error_class": self.canonical_class.value,
            "message": self.message,
            "provider_code": self.provider_code,
            "provider_http_status": self.provider_http_status,
            "provider_request_id": self.provider_request_id,
            "recovery_hint": self.recovery_hint,
            "retryable": self.retryable,
            "details": self.details,
        }


class UnsupportedOperation(CfKanbanProviderError):
    def __init__(self, operation: str) -> None:
        super().__init__(
            canonical_class=CfKanbanErrorClass.VALIDATION_ERROR,
            message=f"Unsupported cfKanban operation: {operation}",
            provider_code="UNSUPPORTED_OPERATION",
            retryable=False,
        )


def _request_id(headers: Mapping[str, str], body: Mapping[str, Any]) -> str | None:
    return str(body.get("request_id") or headers.get("X-Request-ID") or "") or None


def normalize_error(
    status: int,
    headers: Mapping[str, str],
    body: object,
    *,
    transport_message: str | None = None,
) -> CfKanbanProviderError:
    """Map only the error categories defined by the Wave 8 matrix."""

    payload: Mapping[str, Any] = body if isinstance(body, Mapping) else {}
    code = str(payload.get("code") or "") or None
    category = str(payload.get("category") or "")
    message = str(payload.get("message") or transport_message or f"cfKanban HTTP {status}")
    recovery = str(payload.get("recovery") or "") or None
    details = (
        dict(payload.get("details") or {}) if isinstance(payload.get("details"), Mapping) else {}
    )
    retryable = bool(payload.get("retryable", False))

    if status == 401 or category == "authentication":
        normalized = CfKanbanErrorClass.AUTH_REQUIRED
    elif status == 403 or category == "authorization":
        normalized = CfKanbanErrorClass.FORBIDDEN
    elif status == 404 or category == "not_found":
        normalized = CfKanbanErrorClass.NOT_FOUND
    elif status == 409 and (code == "VERSION_CONFLICT" or category == "conflict"):
        normalized = CfKanbanErrorClass.CONFLICT
    elif status == 409 and (
        category == "business_quota" or (code or "").endswith("_LIMIT_REACHED")
    ):
        normalized = CfKanbanErrorClass.QUOTA_EXCEEDED
    elif status == 429 or code == "RATE_LIMITED":
        normalized = CfKanbanErrorClass.RATE_LIMITED
    elif status == 503 and code == "PLATFORM_QUOTA_EXCEEDED":
        normalized = CfKanbanErrorClass.QUOTA_EXCEEDED
    elif status == 503 and (code == "PLATFORM_UNAVAILABLE" or category == "platform_failure"):
        normalized = CfKanbanErrorClass.UPSTREAM_UNAVAILABLE
    elif status == 400 or category == "validation":
        normalized = CfKanbanErrorClass.VALIDATION_ERROR
    elif 500 <= status < 600:
        normalized = CfKanbanErrorClass.UPSTREAM_UNAVAILABLE
    else:
        normalized = CfKanbanErrorClass.UNKNOWN

    return CfKanbanProviderError(
        canonical_class=normalized,
        message=message,
        provider_code=code,
        provider_http_status=status,
        provider_request_id=_request_id(headers, payload),
        recovery_hint=recovery,
        retryable=retryable,
        details=details,
    )
