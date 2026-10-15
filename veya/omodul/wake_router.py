"""veya/omodul/wake_router — one intake for every wake source (NANO-W10 §2).

Every wake source (a user message, the routine clock, a W9 event trigger, a W7
channel post, a runner result, a sub-agent completion) funnels through this
module. It:

1. dedupes by ``compute_fingerprint_for`` (the §5.5 MUST), so the same wake
   never reaches the user twice;
2. classifies the outcome with the pure :mod:`veya.oskill.pure.wake_policy`;
3. hands the result back to layer4, which owns delivery (push P5, channels W7,
   the W7 morning digest).

**It is not a scheduler.** Triggers stay on the existing routine clock, and it
adds no store: the dedupe set is injected by layer4.

Composition (§3): two oprim atoms (:mod:`veya.oprim.turn_envelope`) through one
oskill (:mod:`veya.oskill.pure.wake_policy`), composed by **existing** oservi
engines (``triage(on_signal)`` + ``alerter``) assembled in layer4 — no new
engine is created here.

Pillars:
    fingerprint    — the same wake is processed once
    decision_trail — why a wake replied / digested / stayed silent
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

from veya.oprim.turn_envelope import envelope_fingerprint
from veya.oskill.pure.wake_policy import worth_bugging_user

_log = logging.getLogger(__name__)

__all__ = [
    "WAKE_SOURCES",
    "WakeRouterConfig",
    "WakeRouterInput",
    "compute_fingerprint_for",
    "wake_router",
]

#: Every source that must pass through this intake. A source outside this list
#: is still accepted, but it is recorded as unknown so a new caller is visible.
WAKE_SOURCES: tuple[str, ...] = (
    "user",
    "routine_clock",
    "event_trigger",
    "channel",
    "runner_result",
    "subagent_done",
)

_FINGERPRINT_FIELDS = ("owner_ref", "source", "signal_id")


@dataclass
class WakeRouterConfig:
    """Injected collaborators. No state lives in this module."""

    #: Per-owner settings: ``{"notification_level": ..., "quiet_hours": {...}}``.
    settings: Callable[[], dict[str, Any]] | None = None
    #: (fingerprint) -> bool — True when this wake was already processed.
    seen_before: Callable[[str], bool] | None = None
    #: (fingerprint, signal) -> None — record acceptance.
    mark_seen: Callable[[str, dict[str, Any]], None] | None = None
    #: Optional trail sink (layer4 ``append_canonical_event``).
    trail_sink: Callable[[dict[str, Any]], Any] | None = None
    #: Local seconds-of-day; caller supplies it (quiet hours are local).
    local_seconds: float = 0.0

    _omodul_name: ClassVar[str] = "wake_router"
    _omodul_version: ClassVar[str] = "1.0.0"
    _fingerprint_fields: ClassVar[set[str]] = set(_FINGERPRINT_FIELDS)
    _enabled_pillars: ClassVar[set[str]] = {"fingerprint", "decision_trail"}


@dataclass
class WakeRouterInput:
    """One wake signal, already pseudonymised by the service layer."""

    owner_ref: str
    source: str
    signal_id: str = ""
    summary: str = ""
    user_directed: bool = False
    urgent: bool = False
    novel: bool = False
    ack_only: bool = False
    payload: dict[str, Any] = field(default_factory=dict)


def compute_fingerprint_for(config: Any, input_data: Any) -> str:
    """Stable dedup digest (§5.5 MUST); ``""`` when unreadable."""

    fields = set(getattr(config, "_fingerprint_fields", set()) or set())
    if not fields or input_data is None:
        return ""
    if hasattr(input_data, "model_dump"):
        data = input_data.model_dump()
    elif hasattr(input_data, "__dict__"):
        data = dict(input_data.__dict__)
    elif isinstance(input_data, dict):
        data = dict(input_data)
    else:
        return ""
    return envelope_fingerprint(
        {key: data.get(key) for key in sorted(fields)}, tuple(sorted(fields))
    )


def _call(fn: Callable[..., Any] | None, default: Any, *args: Any) -> Any:
    if fn is None:
        return default
    try:
        return fn(*args)
    except Exception:
        _log.debug("wake_router collaborator failed", exc_info=True)
        return default


def _record(
    config: WakeRouterConfig, trail: list[dict[str, Any]], event: str, **fields: Any
) -> None:
    entry = {"event": event, **fields}
    trail.append(entry)
    sink = config.trail_sink
    if sink is None:
        return
    with contextlib.suppress(Exception):
        result = sink(entry)
        if asyncio.iscoroutine(result):
            result.close()


def wake_router(
    config: WakeRouterConfig,
    input_data: WakeRouterInput,
    output_dir: Path | None = None,
) -> dict[str, Any]:
    """Dedupe + classify one wake signal.

    Never raises (§5.6); a malformed signal returns ``status="failed"``.

    Args:
        config: injected settings + dedupe handles.
        input_data: one wake signal.
        output_dir: optional directory for a trail artifact.

    Returns:
        ``{status, error, outcome, reason, fingerprint, duplicate, source,
        known_source, deliver, decision_trail, trail_path}``. ``deliver`` is the
        instruction layer4 follows — push/channels only for ``reply_now``.
    """

    trail: list[dict[str, Any]] = []
    signal: dict[str, Any] = {
        "user_directed": bool(getattr(input_data, "user_directed", False)),
        "urgent": bool(getattr(input_data, "urgent", False)),
        "novel": bool(getattr(input_data, "novel", False)),
        "ack_only": bool(getattr(input_data, "ack_only", False)),
    }
    source = str(getattr(input_data, "source", "") or "")
    known_source = source in WAKE_SOURCES
    fingerprint = compute_fingerprint_for(config, input_data)

    def outcome(status: str, code: str, reason: str, *, duplicate: bool = False) -> dict[str, Any]:
        return {
            "status": status,
            "error": None if status == "completed" else {"code": code, "reason": reason},
            "outcome": "silent",
            "reason": reason,
            "fingerprint": fingerprint,
            "duplicate": duplicate,
            "source": source,
            "known_source": known_source,
            "deliver": [],
            "decision_trail": trail,
            "trail_path": "",
        }

    if not getattr(input_data, "owner_ref", ""):
        _record(config, trail, "wake.failed", code="INVALID_INPUT", reason="owner_ref is required")
        return outcome("failed", "INVALID_INPUT", "owner_ref is required")

    if not source:
        _record(config, trail, "wake.failed", code="INVALID_INPUT", reason="source is required")
        return outcome("failed", "INVALID_INPUT", "source is required")

    if _call(config.seen_before, False, fingerprint):
        _record(config, trail, "wake.duplicate", fingerprint=fingerprint)
        return outcome("completed", "", "this wake was already processed", duplicate=True)

    settings = _call(config.settings, {}) or {}
    decision = worth_bugging_user(signal, settings, now=config.local_seconds)
    _record(
        config,
        trail,
        "wake.classified",
        source=source,
        outcome=decision["outcome"],
        reason=decision["reason"],
    )
    _call(config.mark_seen, None, fingerprint, {"source": source, "outcome": decision["outcome"]})

    # Push (P5) and channels (W7) fire **only** for reply_now; digest reuses
    # the existing W7 morning feed; emoji_ack is a reaction, not a message.
    deliver = ["push", "channel"] if decision["outcome"] == "reply_now" else []
    if decision["outcome"] == "digest":
        deliver = ["digest"]

    return {
        "status": "completed",
        "error": None,
        "outcome": decision["outcome"],
        "reason": decision["reason"],
        "fingerprint": fingerprint,
        "duplicate": False,
        "source": source,
        "known_source": known_source,
        "deliver": deliver,
        "decision_trail": trail,
        "trail_path": _write_trail(output_dir, trail),
    }


def _write_trail(output_dir: Path | None, trail: list[dict[str, Any]]) -> str:
    if output_dir is None:
        return ""
    try:
        path = Path(output_dir)
        path.mkdir(parents=True, exist_ok=True)
        target = path / "wake_router_trail.jsonl"
        with target.open("a", encoding="utf-8") as handle:
            for entry in trail:
                handle.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
        return str(target)
    except Exception:
        _log.debug("wake_router trail write failed", exc_info=True)
        return ""
