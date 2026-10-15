"""veya/omodul/live_trace — live tool-result stream + plan list (W4).

One transaction: given the existing per-request stream, project the raw
``fire_step`` events into the steps the UI shows, keep the current plan list,
and route a correction typed while the run is live through the **item-1** turn
queue so it lands as a steer.

**No second stream and no second event store.** The events come from the
existing ``server/events.py`` ``on_step`` callback; the state this module keeps
is the same per-request scratch the SSE handler already holds, handed in as an
injected handle.

Pillars:
    decision_trail — why a correction was steered, queued or rejected
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

from veya.oskill.pure.live_trace import correction_action, plan_rows, step_from_tool_event
from veya.oskill.pure.turn_priority import STATE_UNCONFIRMED

_log = logging.getLogger(__name__)

__all__ = [
    "LiveTraceConfig",
    "LiveTraceInput",
    "LiveTraceState",
    "compute_fingerprint_for",
    "live_trace",
]

_FINGERPRINT_FIELDS = ("task_id", "seq", "type")


@dataclass
class LiveTraceConfig:
    """Injected collaborators. No state lives in this module."""

    #: () -> "none" | "pending" | "running" for the task being corrected.
    run_state: Callable[[], str] | None = None
    #: (text) -> str : delivers a steer into the running turn (item 1's queue).
    deliver_steer: Callable[[str], str] | None = None
    #: (fingerprint) -> bool : True when this event was already projected.
    seen_before: Callable[[str], bool] | None = None
    #: Optional trail sink (layer4 ``append_canonical_event``).
    trail_sink: Callable[[dict[str, Any]], Any] | None = None
    #: Cap on steps kept per task; past it the oldest are dropped, counted.
    max_steps: int = 400

    _omodul_name: ClassVar[str] = "live_trace"
    _omodul_version: ClassVar[str] = "1.0.0"
    _fingerprint_fields: ClassVar[set[str]] = set(_FINGERPRINT_FIELDS)
    _enabled_pillars: ClassVar[set[str]] = {"decision_trail"}


@dataclass
class LiveTraceInput:
    """One raw stream event, or one correction the user typed."""

    task_id: str = ""
    #: Raw event dict for the stream path.
    event: dict[str, Any] = field(default_factory=dict)
    #: Correction payload for the correction path.
    correction: dict[str, Any] = field(default_factory=dict)
    #: Monotonic step number the caller assigned to the event.
    seq: int = 0


@dataclass
class LiveTraceState:
    """The projection layer4 hands back to the UI and keeps per request."""

    steps: list[dict[str, Any]] = field(default_factory=list)
    plan: list[dict[str, str]] = field(default_factory=list)
    dropped: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "steps": list(self.steps),
            "plan": list(self.plan),
            "dropped": self.dropped,
        }


def compute_fingerprint_for(config: Any, input_data: Any) -> str:
    """Stable dedup digest for one stream event; ``""`` when unreadable."""

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
    from veya.oprim.turn_envelope import envelope_fingerprint

    return envelope_fingerprint(
        {key: data.get(key) for key in sorted(fields)}, tuple(sorted(fields))
    )


def _call(fn: Callable[..., Any] | None, default: Any, *args: Any) -> Any:
    if fn is None:
        return default
    try:
        return fn(*args)
    except Exception:
        _log.debug("live_trace collaborator failed", exc_info=True)
        return default


def _record(
    config: LiveTraceConfig, trail: list[dict[str, Any]], event: str, **fields: Any
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


def live_trace(
    config: LiveTraceConfig,
    input_data: LiveTraceInput,
    state: LiveTraceState | None = None,
) -> dict[str, Any]:
    """Project one stream event, or land one correction.

    Never raises (§5.6); a malformed input returns ``status="failed"``.

    Args:
        config: injected steer/dedupe handles + trail sink.
        input_data: one raw event, or one correction.
        state: the caller's per-task projection; a fresh one is created when
            ``None`` so a stateless caller still gets a usable result.

    Returns:
        ``{status, error, state, action, step, correction_state, trail}``.
        ``state`` is the updated :class:`LiveTraceState` projection.
    """

    trail: list[dict[str, Any]] = []
    current = state if state is not None else LiveTraceState()
    fingerprint = compute_fingerprint_for(config, input_data)

    correction = dict(getattr(input_data, "correction", {}) or {})
    if correction:
        return _apply_correction(config, correction, current, trail)
    return _apply_event(config, input_data, current, trail, fingerprint)


def _apply_correction(
    config: LiveTraceConfig,
    correction: dict[str, Any],
    current: LiveTraceState,
    trail: list[dict[str, Any]],
) -> dict[str, Any]:
    run_state = _call(config.run_state, "none")
    decision = correction_action(correction, run_state=run_state)
    delivered = ""
    if decision["action"] == "steer":
        text = str(correction.get("text") or "")
        delivered = _call(config.deliver_steer, "delivered", text) or "delivered"
        # The steer receipt is recorded, never assumed: a delivery that threw
        # above degrades to unconfirmed rather than claiming success.
        if delivered in ("", "failed", "error"):
            decision = {**decision, "state": STATE_UNCONFIRMED, "action": "reject"}
    _record(
        config,
        trail,
        "correction.landed",
        action=decision["action"],
        state=decision["state"],
        reason=decision["reason"],
    )
    return {
        "status": "completed",
        "error": None,
        "state": current,
        "action": decision["action"],
        "step": {
            "type": "correction",
            "tool_name": "",
            "detail": str(correction.get("text") or "")[:400],
            "status": decision["state"],
            "seq": correction.get("seq"),
        },
        "correction_state": decision["state"],
        "trail": trail,
    }


def _apply_event(
    config: LiveTraceConfig,
    input_data: LiveTraceInput,
    current: LiveTraceState,
    trail: list[dict[str, Any]],
    fingerprint: str,
) -> dict[str, Any]:
    raw = dict(getattr(input_data, "event", {}) or {})
    if not raw:
        _record(config, trail, "stream.empty", code="INVALID_INPUT", reason="no event payload")
        return {
            "status": "failed",
            "error": {"code": "INVALID_INPUT", "reason": "no event payload"},
            "state": current,
            "action": "drop",
            "step": None,
            "correction_state": STATE_UNCONFIRMED,
            "trail": trail,
        }

    event = dict(raw)
    if not event.get("seq"):
        event["seq"] = int(getattr(input_data, "seq", 0) or 0)
    step = step_from_tool_event(event)
    if step is None:
        # An event type this UI does not show is not an error; it is simply
        # not a step. Recording it keeps the projection honest.
        _record(config, trail, "stream.ignored", type=str(event.get("type") or ""))
        return {
            "status": "completed",
            "error": None,
            "state": current,
            "action": "ignore",
            "step": None,
            "correction_state": STATE_UNCONFIRMED,
            "trail": trail,
        }

    if fingerprint and _call(config.seen_before, False, fingerprint):
        _record(config, trail, "stream.duplicate", fingerprint=fingerprint)
        return {
            "status": "completed",
            "error": None,
            "state": current,
            "action": "drop",
            "step": step,
            "duplicate": True,
            "correction_state": STATE_UNCONFIRMED,
            "trail": trail,
        }

    current.steps.append(step)
    if step["type"] == "plan_update":
        current.plan = plan_rows(event.get("todos"))
    # Bounded: the cap drops the OLDEST steps and counts the drops, because a
    # silently unbounded list is a memory leak dressed as a feature.
    while len(current.steps) > int(config.max_steps):
        current.steps.pop(0)
        current.dropped += 1

    _record(config, trail, "stream.step", type=step["type"], tool_name=step.get("tool_name", ""))
    return {
        "status": "completed",
        "error": None,
        "state": current,
        "action": "append",
        "step": step,
        "duplicate": False,
        "correction_state": STATE_UNCONFIRMED,
        "trail": trail,
    }


def _write_trail(output_dir: Path | None, trail: list[dict[str, Any]]) -> str:
    """Optional trail artifact; the live path streams instead of writing."""

    if output_dir is None:
        return ""
    try:
        path = Path(output_dir)
        path.mkdir(parents=True, exist_ok=True)
        target = path / "live_trace_trail.jsonl"
        with target.open("a", encoding="utf-8") as handle:
            for entry in trail:
                handle.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
        return str(target)
    except Exception:
        _log.debug("live_trace trail write failed", exc_info=True)
        return ""
