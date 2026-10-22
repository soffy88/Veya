"""Brain circuit breaker (W12 Part B, flag VEYA_BRAIN_BREAKER=1).

The main-brain pool (opencode-go → NIM → OpenRouter free) previously
retried every tier on every turn; when the whole pool is exhausted the
turn waited the full per-call timeout. This module adds, inside the
existing router (``veya/obase/llm.py`` — no new registry):

* a breaker per provider. A provider answering 429/quota/limit opens
  its breaker for a cooldown (monthly/daily limit → until the next UTC
  day boundary; plain 429 → 10 min, doubling to 2 h on repeat).
* an open breaker makes the router skip that tier; one cheap re-probe
  happens when the cooldown ends.
* when every tier's breaker is open, ``llm_call`` fails fast with the
  existing friendly reply instead of a full timeout.

State is per-process, thread-safe, and holds no secrets. All entry
points are flag-gated: with the flag off (the default) this module is
inert and the router behaves exactly as before.
"""

from __future__ import annotations

import os
import threading
import time
from datetime import UTC, datetime, timedelta
from typing import Any

_BREAKER_ENV = "VEYA_BRAIN_BREAKER"

#: Plain 429 cooldown: 10 min, doubling per repeat up to 2 h.
_BASE_COOLDOWN_S = 600.0
_MAX_COOLDOWN_S = 7200.0

#: A monthly/daily quota error stays open until the next UTC day boundary.
_QUOTA_MARKERS = ("monthly", "daily", "limitname", "quota", "insufficient", "usagelimit")

#: Hard-failure cooldowns (P1-1b): auth/connect breakers recover as soon as the
#: key is rotated or the endpoint is back, so they stay short.
_AUTH_BREAK_COOLDOWN_S = 600.0
_CONNECT_BREAK_COOLDOWN_S = 120.0

_lock = threading.RLock()
#: provider -> breaker record.
_breakers: dict[str, dict[str, Any]] = {}


def _flag_on() -> bool:
    return os.environ.get(_BREAKER_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


def classify_quota_error(text: str) -> str:
    """``monthly``/``daily``/``plain`` — the cooldown class of an error body.

    The observed opencode-go failure answers
    ``429 GoUsageLimitError limitName=monthly``; a monthly/daily limit
    cannot clear before the next UTC day, so those cool down to the day
    boundary. Anything else that still says 429/rate-limit is a plain
    transient limit.
    """
    lowered = (text or "").lower()
    if "monthly" in lowered:
        return "monthly"
    if "daily" in lowered:
        return "daily"
    if "429" in lowered or ("rate" in lowered and "limit" in lowered):
        return "plain"
    return ""


def _next_utc_day_boundary(now: float | None = None) -> float:
    moment = datetime.fromtimestamp(now if now is not None else time.time(), tz=UTC)
    nxt = (moment + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return float(nxt.timestamp())


def _cooldown_for(kind: str, *, repeats: int, now: float | None = None) -> float:
    """Cooldown seconds for one breaker class."""
    moment = now if now is not None else time.time()
    if kind in ("monthly", "daily"):
        return max(0.0, _next_utc_day_boundary(moment) - moment)
    base = _BASE_COOLDOWN_S * float(2 ** max(0, repeats - 1))
    return min(base, float(_MAX_COOLDOWN_S))


def _is_quota_error(exc: Any, status: int | None = None) -> tuple[bool, str]:
    """Does this failure look like a 429/quota/limit answer?"""
    text = ""
    response = getattr(exc, "response", None)
    if response is not None and status is None:
        status = getattr(response, "status_code", None)
    if response is not None:
        try:
            text = str(response.text or "")
        except Exception:
            text = ""
    if not text:
        text = str(exc or "")
    if status is not None and int(status) == 429:
        return True, classify_quota_error(text) or "plain"
    lowered = text.lower()
    if any(marker in lowered for marker in _QUOTA_MARKERS):
        return True, classify_quota_error(text) or "plain"
    return False, ""


def record_provider_failure(provider: str, exc: Any, *, now: float | None = None) -> None:
    """Open (or extend) the breaker for ``provider`` on a quota answer.

    No-op unless the brain-breaker flag is on, and never raises: a
    breaker is best-effort bookkeeping, not a gate.
    """
    if not _flag_on():
        return
    quota, kind = _is_quota_error(exc)
    if not quota:
        return
    moment = now if now is not None else time.time()
    with _lock:
        record = _breakers.get(provider) or {"repeats": 0, "last_kind": ""}
        repeats = record["repeats"] + (1 if record.get("last_kind") == kind else 0) + 1
        _breakers[provider] = {
            "state": "open",
            "opened_at": moment,
            "reopen_at": moment + _cooldown_for(kind, repeats=repeats, now=moment),
            "repeats": repeats,
            "last_kind": kind,
            "reason": kind,
        }


def breaker_state(provider: str, *, now: float | None = None) -> dict[str, Any] | None:
    """Read-only breaker view for one provider (health route).

    Returns a copy; the ``state`` field is widened from ``open`` to
    ``half_open`` when the cooldown has elapsed, without mutating the
    stored record (the next successful call closes it).
    """
    moment = now if now is not None else time.time()
    with _lock:
        record = _breakers.get(provider)
        if record is None:
            return None
        view = dict(record)
        if view.get("state") == "open" and float(view.get("reopen_at") or 0) <= moment:
            view["state"] = "half_open"
        return view


def snapshot(now: float | None = None) -> list[dict[str, Any]]:
    """Every breaker, provider-sorted, no secrets."""
    moment = now if now is not None else time.time()
    with _lock:
        views = [
            {"provider": provider, **(breaker_state(provider, now=moment) or {})}
            for provider in sorted(_breakers)
        ]
    return views


def breaker_open(provider: str, *, now: float | None = None) -> bool:
    """Should the router skip this provider right now?"""
    if not _flag_on():
        return False
    moment = now if now is not None else time.time()
    with _lock:
        record = _breakers.get(provider)
        if record is None or record.get("state") != "open":
            return False
        return float(record.get("reopen_at") or 0) > moment


def note_provider_break(provider: str, kind: str = "", *, now: float | None = None) -> None:
    """Open a short breaker for a non-quota hard failure (auth/connect).

    ``record_provider_failure`` only reacts to quota answers. A provider whose
    key answers 401/403, or whose endpoint refuses connections, is equally
    dead for the rest of the turn, so the router can skip it without paying a
    per-tier timeout. The cooldown is deliberately short: unlike a quota limit,
    a rotated key or a restarted gateway becomes healthy again within minutes.

    No-op unless the flag is on, and never raises.
    """
    if not _flag_on() or not provider:
        return
    cooldown = _AUTH_BREAK_COOLDOWN_S if kind == "auth" else _CONNECT_BREAK_COOLDOWN_S
    moment = now if now is not None else time.time()
    with _lock:
        _breakers[provider] = {
            "state": "open",
            "opened_at": moment,
            "reopen_at": moment + cooldown,
            "repeats": 1,
            "last_kind": kind or "hard",
            "reason": f"hard-failure:{kind or 'unknown'}",
        }


def note_provider_success(provider: str) -> None:
    """A provider answered — close its breaker (re-probe succeeded)."""
    if not _flag_on():
        return
    with _lock:
        _breakers.pop(provider, None)


def all_tiers_open(providers: list[str], *, now: float | None = None) -> bool:
    """Is every listed tier skipped right now?"""
    if not providers:
        return False
    return all(breaker_open(p, now=now) for p in providers)


def enabled() -> bool:
    """Is the brain-breaker feature flag on?"""
    return _flag_on()


def reset() -> None:
    """Test hook: drop all breaker state."""
    with _lock:
        _breakers.clear()


__all__ = [
    "all_tiers_open",
    "breaker_open",
    "breaker_state",
    "classify_quota_error",
    "enabled",
    "note_provider_break",
    "note_provider_success",
    "record_provider_failure",
    "reset",
    "snapshot",
]
