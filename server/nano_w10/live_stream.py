"""server/nano_w10/live_stream — W4 live tool-result stream + correction box.

Binds :mod:`veya.omodul.live_trace` to the two things that already exist:

* the per-request ``on_step`` callback from :mod:`server.events` (the same one
  the SSE route already installs) — this module only *projects* what arrives,
  it never starts a stream or writes an event;
* the item-1 turn queue (:mod:`server.nano_w10.turn_queue`) — a correction
  typed while a run is live lands through its steer path, so there is no
  second control channel.

Gated by ``VEYA_LIVE_STREAM`` (default **on** — the spec allows the live
stream UI to be on; it only changes what the user already sees, and with the
flag off every event simply projects to no step).
"""

from __future__ import annotations

import itertools
import logging
import threading
import time
from collections.abc import Callable
from typing import Any

from server.nano_w10.flags import flag_enabled
from veya.omodul.live_trace import (
    LiveTraceConfig,
    LiveTraceInput,
    LiveTraceState,
    live_trace,
)

_log = logging.getLogger("veya.nano_w10.live_stream")

LIVE_STREAM_FLAG = "VEYA_LIVE_STREAM"

#: How long an event fingerprint is remembered for client redelivery.
_SEEN_RETENTION_S = 3600.0


def live_stream_enabled() -> bool:
    """Is the live tool-result stream on? (spec W4: may be on)."""

    return flag_enabled(LIVE_STREAM_FLAG, default=True)


class LiveStream:
    """Per-task projection of the existing stream, plus the correction box.

    Parameters
    ----------
    push:
        ``(step) -> Any`` — the caller's existing SSE writer. Injected so this
        module never owns a connection or a transport.
    turn_state:
        ``(task_id) -> str`` — the item-1 queue's view of the conversation.
    deliver_steer:
        ``(task_id, text) -> str`` — the item-1 steer path.
    """

    def __init__(
        self,
        *,
        push: Callable[[dict[str, Any]], Any] | None = None,
        turn_state: Callable[[str], str] | None = None,
        deliver_steer: Callable[[str, str], str] | None = None,
        max_steps: int = 400,
    ) -> None:
        self._push = push
        self._turn_state = turn_state
        self._deliver_steer = deliver_steer
        self._max_steps = int(max_steps)
        self._states: dict[str, LiveTraceState] = {}
        self._seen: dict[str, float] = {}
        self._lock = threading.RLock()
        self._seq = itertools.count(1)

    # ── injected state handles ──────────────────────────────────────────

    def _seen_before(self, fingerprint: str) -> bool:
        if fingerprint in self._seen:
            return True
        cutoff = time.time() - _SEEN_RETENTION_S
        for stale in [key for key, at in self._seen.items() if at < cutoff]:
            self._seen.pop(stale, None)
        return False

    # ── public surface ──────────────────────────────────────────────────

    def state(self, task_id: str) -> LiveTraceState:
        """The current projection for one task (created empty on first use)."""

        with self._lock:
            return self._states.setdefault(str(task_id or ""), LiveTraceState())

    def on_step(self, task_id: str, event: dict[str, Any]) -> dict[str, Any] | None:
        """Project one raw stream event and push it if it is a visible step.

        Returns the omodul result, or ``None`` when the live stream is off —
        the caller then does exactly what it did before.
        """

        if not live_stream_enabled():
            return None
        result = self._project(task_id, event=event)
        step = result.get("step") if result.get("action") == "append" else None
        if step and self._push is not None:
            try:
                self._push(step)
            except Exception:
                # A dead SSE connection must never abort the run.
                _log.debug("live stream push failed", exc_info=True)
        return result

    def correction(
        self,
        task_id: str,
        text: str,
        *,
        external_id: str = "",
    ) -> dict[str, Any]:
        """Land a correction typed while the run is live.

        Goes through the item-1 queue: with a running turn it becomes a steer
        at the next safe point; with a pending turn it queues; with no turn it
        is refused as "no run to correct" rather than silently starting one.
        """

        if not live_stream_enabled():
            return {
                "status": "failed",
                "error": {"code": "FEATURE_DISABLED", "flag": LIVE_STREAM_FLAG},
                "action": "reject",
                "correction_state": "unconfirmed",
                "reason": f"{LIVE_STREAM_FLAG} is disabled",
                "trail": [],
            }
        return self._project(
            task_id,
            correction={"text": text, "external_id": external_id, "seq": next(self._seq)},
        )

    def snapshot(self, task_id: str) -> dict[str, Any]:
        """Everything the UI would render for one task right now."""

        state = self.state(task_id)
        return state.to_dict()

    def _project(self, task_id: str, **payload: Any) -> dict[str, Any]:
        state = self.state(task_id)
        result = live_trace(
            LiveTraceConfig(
                run_state=lambda: (self._turn_state or (lambda _tid: "none"))(task_id),
                deliver_steer=lambda text: (
                    self._deliver_steer(task_id, text) if self._deliver_steer else "delivered"
                ),
                seen_before=self._seen_before,
                max_steps=self._max_steps,
            ),
            LiveTraceInput(task_id=task_id, seq=next(self._seq), **payload),
            state,
        )
        if result.get("action") == "append":
            fingerprint = result.get("step") or {}
            self._seen[f"{task_id}:{fingerprint.get('seq')}"] = time.time()
        return result


_STREAM: LiveStream | None = None
_STREAM_LOCK = threading.Lock()


def get_live_stream() -> LiveStream:
    """The process-wide :class:`LiveStream`."""

    global _STREAM
    if _STREAM is None:
        with _STREAM_LOCK:
            if _STREAM is None:
                _STREAM = LiveStream()
    return _STREAM


def reset_live_stream() -> None:
    """Drop the process stream (tests / operator reset)."""

    global _STREAM
    _STREAM = None


def wire_to_turn_queue(queue: Any, *, push: Callable[[dict[str, Any]], Any] | None = None) -> LiveStream:
    """Bind a live stream to an item-1 :class:`TurnQueue`.

    The correction box and an inbound direct message then share **one** path:
    both go through ``queue.steer``, which is the item-1 steer with its W6 K
    receipt. No second control channel is created here.

    Args:
        queue: the item-1 turn queue (or anything exposing
            ``turn_state(conversation_ref)`` and ``steer(...)``).
        push: the caller's existing SSE writer.

    Returns:
        The configured :class:`LiveStream`.
    """

    return LiveStream(
        push=push,
        turn_state=lambda conversation_ref: queue.turn_state(conversation_ref),
        deliver_steer=lambda conversation_ref, text: (
            queue.steer(conversation_ref, text).get("state", "delivered")
        ),
    )


__all__ = [
    "LIVE_STREAM_FLAG",
    "LiveStream",
    "get_live_stream",
    "live_stream_enabled",
    "reset_live_stream",
    "wire_to_turn_queue",
]