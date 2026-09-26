"""Hicode model identity and upstream quota-cooldown authority."""

from __future__ import annotations

import json
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

FAILURE_CLASS = "UPSTREAM_QUOTA_EXHAUSTED"
_AUTHORITY = Path(__file__).resolve().parents[1] / "config" / "hicode_model_mapping.json"
_RESET_RE = re.compile(r"resets?\s+in\s+(?:(\d+)h)?\s*(?:(\d+)m)?\s*(?:(\d+)s)?", re.I)
_COOLDOWNS: dict[tuple[str, str, str], dict[str, Any]] = {}


def load_hicode_mapping_authority() -> dict[str, dict[str, Any]]:
    raw = json.loads(_AUTHORITY.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or len(raw) != 1:
        raise RuntimeError("HICODE mapping authority must contain exactly one mapping")
    out: dict[str, dict[str, Any]] = {}
    for key, value in raw.items():
        if not isinstance(value, dict) or value.get("requested_model") != key:
            raise RuntimeError("HICODE mapping authority has an invalid requested_model")
        required = ("internal_provider", "upstream_model", "source", "active", "updated_at")
        if any(name not in value for name in required):
            raise RuntimeError("HICODE mapping authority is incomplete")
        out[key] = dict(value)
    return out


def hicode_model_mapping(requested_model: str) -> dict[str, Any]:
    mapping = load_hicode_mapping_authority().get(requested_model)
    if not mapping or not mapping.get("active"):
        raise RuntimeError(f"inactive or unknown HICODE model: {requested_model}")
    return mapping


def _flatten(value: Any) -> str:
    if isinstance(value, Mapping):
        return " ".join(f"{key} {_flatten(item)}" for key, item in value.items())
    if isinstance(value, (list, tuple)):
        return " ".join(_flatten(item) for item in value)
    return str(value or "")


def _number(value: Any) -> int | None:
    try:
        result = int(value)
    except (TypeError, ValueError):
        return None
    return result if result >= 0 else None


def _parse_reset_seconds(text: str) -> int | None:
    match = _RESET_RE.search(text)
    if not match:
        return None
    return sum(
        int(value or 0) * multiplier
        for value, multiplier in zip(match.groups(), (3600, 60, 1), strict=True)
    )


def _parse_reset_time(value: Any, *, observed_at: float) -> int | None:
    if isinstance(value, (int, float)):
        delta = int(value - observed_at)
        return delta if delta >= 0 else None
    text = str(value or "").strip()
    if not text:
        return None
    try:
        when = datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None
    delta = int(when - observed_at)
    return delta if delta >= 0 else None


@dataclass(frozen=True)
class UpstreamQuotaFailure:
    failure_class: str = FAILURE_CLASS
    retryable_immediately: bool = False
    provider: str | None = None
    model: str | None = None
    credential_scope: str | None = None
    reset_seconds: int | None = None
    upstream_reset_seconds: int | None = None
    reset_time: str | None = None
    local_reset_seconds: int | None = None
    upstream_evidence: str = ""
    observed_at: float = field(default_factory=time.time)

    @property
    def cooldown_until(self) -> float:
        return effective_cooldown_until(
            self.observed_at,
            self.local_reset_seconds,
            upstream_reset_seconds=self.upstream_reset_seconds,
        )


class ModelCooldown(RuntimeError):
    """Terminal caller-facing state for an exhausted upstream quota."""

    def __init__(self, failure: UpstreamQuotaFailure):
        super().__init__(f"model cooldown: retry not before {failure.cooldown_until:.3f}")
        self.failure = failure
        self.failure_class = failure.failure_class
        self.retryable_immediately = False
        self.retry_not_before = failure.cooldown_until
        self.provider = failure.provider
        self.model = failure.model
        record_cooldown(failure)


def classify_upstream_failure(payload: Any) -> UpstreamQuotaFailure | None:
    text = _flatten(payload)
    upper = text.upper()
    status = payload.get("status_code") if isinstance(payload, Mapping) else None
    code = str(payload.get("code") or "").lower() if isinstance(payload, Mapping) else ""
    quota = (
        status == 429
        or "RESOURCE_EXHAUSTED" in upper
        or code in {"model_cooldown", "quota_exceeded", "resource_exhausted"}
        or "quota exceeded" in text.lower()
    )
    if not quota:
        return None
    reset = _number(payload.get("reset_seconds")) if isinstance(payload, Mapping) else None
    upstream_error = (
        _flatten(payload.get("last_upstream_error")) if isinstance(payload, Mapping) else text
    )
    observed_at = time.time()
    reset_time = (
        str(payload.get("reset_time") or "") or None if isinstance(payload, Mapping) else None
    )
    upstream_reset = (
        reset
        or _parse_reset_seconds(upstream_error)
        or _parse_reset_seconds(text)
        or _parse_reset_time(reset_time, observed_at=observed_at)
    )
    return UpstreamQuotaFailure(
        provider=str(payload.get("provider") or "") or None
        if isinstance(payload, Mapping)
        else None,
        model=str(payload.get("model") or "") or None if isinstance(payload, Mapping) else None,
        credential_scope=str(payload.get("credential_scope") or "") or None
        if isinstance(payload, Mapping)
        else None,
        reset_seconds=reset,
        local_reset_seconds=_number(payload.get("local_reset_seconds"))
        if isinstance(payload, Mapping)
        else None,
        upstream_reset_seconds=upstream_reset,
        reset_time=reset_time,
        upstream_evidence=upstream_error[:4000],
        observed_at=observed_at,
    )


def effective_cooldown_until(
    observed_at: float,
    local_reset_seconds: int | None,
    *,
    upstream_reset_seconds: int | None = None,
    now: float | None = None,
) -> float:
    del now
    local_until = observed_at + max(0, local_reset_seconds or 0)
    upstream_until = observed_at + max(0, upstream_reset_seconds or 0)
    return max(local_until, upstream_until)


def record_cooldown(failure: UpstreamQuotaFailure) -> dict[str, Any]:
    """Persist the in-process cooldown projection without retaining secrets."""

    key = (
        failure.provider or "unknown",
        failure.model or "unknown",
        failure.credential_scope or "default",
    )
    state = {
        "provider": failure.provider,
        "model": failure.model,
        "credential_scope": failure.credential_scope,
        "failure_class": failure.failure_class,
        "observed_at": failure.observed_at,
        "local_reset_seconds": failure.local_reset_seconds,
        "upstream_reset_seconds": failure.upstream_reset_seconds,
        "reset_time": failure.reset_time,
        "cooldown_until": effective_cooldown_until(
            failure.observed_at,
            failure.local_reset_seconds,
            upstream_reset_seconds=failure.upstream_reset_seconds,
        ),
    }
    _COOLDOWNS[key] = state
    return dict(state)


def cooldown_snapshot() -> list[dict[str, Any]]:
    return [dict(value) for value in _COOLDOWNS.values()]
