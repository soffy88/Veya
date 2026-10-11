"""veya/omodul/turn_intake — one inbound message → one turn decision (NANO-W10 §1).

A business transaction with the standard 3O contract: signature
``(config, input_data, output_dir) -> dict`` returning ``status``/``error``,
never raising (except ``CancelledError``), with ``_enabled_pillars`` declared.

It owns **no** scheduler and **no** task store. Given one inbound envelope it
decides what should happen — start / queue / steer, and whether a pending
burst collapses into one turn — and hands the caller back the members of the
turn to open. Opening the turn stays layer4, because that is where the existing
product task projection lives.

Composition (§3): it composes two oprim atoms
(:mod:`veya.oprim.turn_envelope`) through one oskill
(:mod:`veya.oskill.pure.turn_priority`), so it clears the "≥2 oprims" bar
without calling a sibling omodul.

State injection (§1.2): the pending list, the idempotency set and the turn
state are all **injected** callables/handles from layer4 — this module holds no
module-level mutable state.

Pillars:
    fingerprint    — one inbound message collapses to one turn
    decision_trail — why this message steered, queued or was dropped
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

from veya.oprim.turn_envelope import envelope_fingerprint, idem_key
from veya.oskill.pure.turn_priority import (
    STATE_UNCONFIRMED,
    classify_inbound,
    coalesce_window,
    envelope_priority,
    order_queue,
    steer_decision,
)

_log = logging.getLogger(__name__)

__all__ = [
    "TurnIntakeConfig",
    "TurnIntakeInput",
    "turn_intake",
    "compute_fingerprint_for",
]

#: Fields the fingerprint covers. ``conversation_ref`` is the stable non-PII
#: reference the service layer produced; a raw user id must never reach here.
_FINGERPRINT_FIELDS = ("conversation_ref", "kind", "external_id")


@dataclass
class TurnIntakeConfig:
    """Injected collaborators. Everything stateful is a handle from layer4."""

    #: () -> current turn state: "none" | "pending" | "running".
    turn_state: Callable[[], str] | None = None
    #: () -> pending envelopes queued but not yet started.
    list_pending: Callable[[], list[dict[str, Any]]] | None = None
    #: (envelope, merged_text) -> None : appends a coalesced turn member.
    append_pending: Callable[[dict[str, Any], str], None] | None = None
    #: (idem_key) -> bool : True when this message was already accepted.
    seen_before: Callable[[str], bool] | None = None
    #: (idem_key, envelope) -> None : records acceptance (conversation store).
    mark_seen: Callable[[str, dict[str, Any]], None] | None = None
    #: (steer_text, bot_ref) -> str : delivers a steer to the running turn.
    deliver_steer: Callable[[str, str], str] | None = None
    #: True when the model produced start evidence for the running turn.
    has_start_evidence: Callable[[], bool] | None = None
    #: Merging window in seconds (spec default 2s, configurable).
    coalesce_window_s: float = 2.0
    #: Deployment salt for the idempotency key.
    salt: str = ""
    #: Optional trail sink (layer4 ``append_canonical_event``).
    trail_sink: Callable[[dict[str, Any]], Any] | None = None

    _omodul_name: ClassVar[str] = "turn_intake"
    _omodul_version: ClassVar[str] = "1.0.0"
    _fingerprint_fields: ClassVar[set[str]] = set(_FINGERPRINT_FIELDS)
    _enabled_pillars: ClassVar[set[str]] = {"fingerprint", "decision_trail"}


@dataclass
class TurnIntakeInput:
    """One inbound message, already pseudonymised by the service layer."""

    conversation_ref: str
    kind: str = "direct"
    text: str = ""
    external_id: str = ""
    arrived_at: float = 0.0
    bot_ref: str = ""
    user_directed: bool = False
    mention: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


def compute_fingerprint_for(config: Any, input_data: Any) -> str:
    """Stable digest for service-layer dedup (§5.5 MUST).

    Args:
        config: reads ``_fingerprint_fields``.
        input_data: the intake input (dataclass, pydantic model or dict).

    Returns:
        24 hex characters, or ``""`` when the fields cannot be read.
    """

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
    envelope = {key: data.get(key) for key in sorted(fields)}
    return envelope_fingerprint(envelope, tuple(sorted(fields)))


def _record(config: TurnIntakeConfig, trail: list[dict[str, Any]], event: str, **fields: Any) -> None:
    """Append one trail step; a failing sink never breaks the transaction."""

    entry = {"event": event, **fields}
    trail.append(entry)
    sink = config.trail_sink
    if sink is None:
        return
    with contextlib.suppress(Exception):
        result = sink(entry)
        if asyncio.iscoroutine(result):
            result.close()


def _call(fn: Callable[..., Any] | None, default: Any, *args: Any) -> Any:
    if fn is None:
        return default
    try:
        return fn(*args)
    except Exception:
        _log.debug("turn_intake collaborator failed", exc_info=True)
        return default


def turn_intake(
    config: TurnIntakeConfig,
    input_data: TurnIntakeInput,
    output_dir: Path | None = None,
    *,
    now: float | None = None,
) -> dict[str, Any]:
    """Decide what happens to one inbound message.

    Never raises: a malformed envelope returns ``status="failed"`` with an
    error code, matching the omodul contract (§5.6).

    Args:
        config: injected collaborators (queue handles, steer delivery, trail).
        input_data: one pseudonymised inbound message.
        output_dir: optional directory for a trail artifact.
        now: current time; defaults to ``input_data.arrived_at``.

    Returns:
        ``{status, error, action, kind, priority, state, reason, idem_key,
        fingerprint, turn_members, text, decision_trail, trail_path}``.
    """

    trail: list[dict[str, Any]] = []
    moment = float(now if now is not None else (getattr(input_data, "arrived_at", 0.0) or 0.0))

    envelope: dict[str, Any] = {
        "kind": getattr(input_data, "kind", "") or "",
        "text": getattr(input_data, "text", "") or "",
        "arrived_at": moment,
        "external_id": getattr(input_data, "external_id", "") or "",
        "conversation_ref": getattr(input_data, "conversation_ref", "") or "",
        "bot_ref": getattr(input_data, "bot_ref", "") or "",
        "user_directed": bool(getattr(input_data, "user_directed", False)),
        "mention": bool(getattr(input_data, "mention", False)),
    }

    def failed(code: str, reason: str) -> dict[str, Any]:
        _record(config, trail, "intake.failed", code=code, reason=reason)
        return {
            "status": "failed",
            "error": {"code": code, "reason": reason},
            "action": "drop",
            "kind": classify_inbound(envelope),
            "priority": envelope_priority(envelope),
            "state": STATE_UNCONFIRMED,
            "reason": reason,
            "idem_key": "",
            "fingerprint": "",
            "turn_members": [],
            "text": "",
            "decision_trail": trail,
            "trail_path": "",
        }

    if not envelope["conversation_ref"]:
        return failed("INVALID_INPUT", "conversation_ref is required")

    key = idem_key(
        str(envelope["conversation_ref"]),
        str(envelope["kind"]),
        str(envelope["external_id"] or moment),
        salt=config.salt,
    )
    fingerprint = compute_fingerprint_for(config, input_data)

    if _call(config.seen_before, False, key):
        _record(config, trail, "intake.duplicate", idem_key=key)
        return {
            "status": "completed",
            "error": None,
            "action": "drop",
            "kind": classify_inbound(envelope),
            "priority": envelope_priority(envelope),
            "state": STATE_UNCONFIRMED,
            "reason": "this inbound message was already accepted (idempotency key seen)",
            "idem_key": key,
            "fingerprint": fingerprint,
            "turn_members": [],
            "text": "",
            "decision_trail": trail,
            "trail_path": "",
        }

    state = str(_call(config.turn_state, "none") or "none")
    decision = steer_decision(
        envelope,
        turn_state=state,
        has_start_evidence=bool(_call(config.has_start_evidence, False)),
    )

    if decision["action"] == "steer":
        delivered = _call(
            config.deliver_steer, STATE_UNCONFIRMED, str(envelope["text"]), str(envelope["bot_ref"])
        )
        receipt = str(delivered or decision["state"])
        _record(
            config,
            trail,
            "intake.steer",
            idem_key=key,
            state=receipt,
            reason=decision["reason"],
        )
        _call(config.mark_seen, None, key, envelope)
        return {
            "status": "completed",
            "error": None,
            "action": "steer",
            "kind": decision["kind"],
            "priority": decision["priority"],
            "state": receipt,
            "reason": decision["reason"],
            "idem_key": key,
            "fingerprint": fingerprint,
            "turn_members": [],
            "text": str(envelope["text"]),
            "decision_trail": trail,
            "trail_path": _write_trail(output_dir, trail),
        }

    pending = list(_call(config.list_pending, []) or [])
    pending.append(envelope)
    merged = coalesce_window(pending, now=moment, window_s=config.coalesce_window_s)

    if merged["coalesced"]:
        _call(config.append_pending, None, envelope, str(merged["text"]))
    else:
        _call(config.append_pending, None, envelope, str(envelope["text"]))

    _record(
        config,
        trail,
        "intake.queued" if int(merged["count"]) <= 1 else "intake.coalesced",
        idem_key=key,
        count=merged["count"],
        priority=merged["priority"],
        reason=merged["reason"],
    )
    _call(config.mark_seen, None, key, envelope)

    ordered = order_queue(merged["members"])
    return {
        "status": "completed",
        "error": None,
        "action": "queue",
        "kind": merged["kind"] or decision["kind"],
        "priority": int(merged["priority"]),
        "state": STATE_UNCONFIRMED,
        "reason": merged["reason"] or decision["reason"],
        "idem_key": key,
        "fingerprint": fingerprint,
        "turn_members": ordered,
        "text": str(merged["text"]),
        "decision_trail": trail,
        "trail_path": _write_trail(output_dir, trail),
    }


def _write_trail(output_dir: Path | None, trail: list[dict[str, Any]]) -> str:
    """Persist the trail next to the other run artifacts (best effort)."""

    if output_dir is None:
        return ""
    try:
        path = Path(output_dir)
        path.mkdir(parents=True, exist_ok=True)
        target = path / "turn_intake_trail.jsonl"
        with target.open("a", encoding="utf-8") as handle:
            for entry in trail:
                handle.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
        return str(target)
    except Exception:
        _log.debug("turn_intake trail write failed", exc_info=True)
        return ""