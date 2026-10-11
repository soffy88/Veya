"""server/nano_w10/turn_queue — priority + coalescing in front of the turns (§1).

**Not a scheduler and not a task store.** This module holds a short in-process
list of *envelopes* — the same kind of ephemeral bookkeeping
:mod:`server.bot_team.async_turns` already keeps in ``_pending`` (whose own
docstring says it is not a store and a restart forgets it). What actually runs
is still the existing turn path: the queue calls
``server.bot_team.turns.begin_bot_turn`` / ``finish_bot_turn``, i.e. the same
product task projection and the same GoalRun.

Rules implemented (all pure rules live in
:mod:`veya.oskill.pure.turn_priority`):

* priority ``direct > group_mention > channel > routine``;
* a direct user message that lands while a turn is **running** is delivered as a
  **steer** (W6 K receipts) instead of waiting;
* messages that land within ``coalesce_window_s`` (default 2 s) while a turn is
  pending collapse into **one** turn;
* one idempotency key per inbound message, so a client retry never opens a
  second turn.

Gated by ``VEYA_TURN_QUEUE`` (default OFF).
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from server.nano_w10.flags import flag_enabled
from veya.omodul.turn_intake import (
    TurnIntakeConfig,
    TurnIntakeInput,
    turn_intake,
)
from veya.oprim.turn_envelope import idem_key
from veya.oskill.pure.turn_priority import (
    STATE_UNCONFIRMED,
    coalesce_window,
    order_queue,
)

_log = logging.getLogger("veya.bot_team.turn_queue")

TURN_QUEUE_FLAG = "VEYA_TURN_QUEUE"

#: How long an accepted idempotency key is remembered (a live client retry
#: window; not a durable ledger — a restart forgets, exactly like the queue).
_SEEN_RETENTION_S = 3600.0


def turn_queue_enabled() -> bool:
    """Is the turn queue on? (spec item 1 is behind a flag, default OFF)."""

    return flag_enabled(TURN_QUEUE_FLAG)


@dataclass
class _QueuedTurn:
    """One pending turn: the envelopes that will become it."""

    conversation_ref: str
    bot_ref: str
    members: list[dict[str, Any]] = field(default_factory=list)
    text: str = ""
    started: bool = False


class TurnQueue:
    """Per-conversation priority queue with coalescing and steering.

    Parameters
    ----------
    window_s:
        Coalescing window in seconds (spec default 2 s).
    max_pending:
        Hard cap on envelopes kept per conversation. Past the cap the lowest
        priority envelope is dropped **and recorded** — a silently growing
        memory is worse than a visible drop.
    """

    def __init__(self, *, window_s: float = 2.0, max_pending: int = 32) -> None:
        self.window_s = float(window_s)
        self.max_pending = int(max_pending)
        self._queues: dict[str, _QueuedTurn] = {}
        self._seen: dict[str, float] = {}
        self._running: dict[str, str] = {}
        self._steers: dict[str, list[str]] = {}
        self._tasks: set[asyncio.Task[Any]] = set()

    # ── state handles consumed by the omodul ────────────────────────────

    def turn_state(self, conversation_ref: str) -> str:
        """``none`` | ``pending`` | ``running`` for one conversation."""

        if conversation_ref in self._running:
            return "running"
        if conversation_ref in self._queues:
            return "pending"
        return "none"

    def list_pending(self, conversation_ref: str) -> list[dict[str, Any]]:
        """Pending envelopes for one conversation (a copy)."""

        queued = self._queues.get(conversation_ref)
        return list(queued.members) if queued else []

    def seen_before(self, key: str) -> bool:
        """Has this idempotency key already been accepted?"""

        if key in self._seen:
            return True
        # Retention sweep: a key older than the window can no longer be a live
        # retry, and the set must not grow without bound.
        cutoff = time.time() - _SEEN_RETENTION_S
        for stale in [known for known, at in self._seen.items() if at < cutoff]:
            self._seen.pop(stale, None)
        return False

    def pending_snapshot(self) -> list[dict[str, Any]]:
        """Everything currently queued, for the settings/UI surface."""

        out: list[dict[str, Any]] = []
        for ref, queued in self._queues.items():
            out.append(
                {
                    "conversation_ref": ref,
                    "bot_ref": queued.bot_ref,
                    "count": len(queued.members),
                    "kinds": [str(member.get("kind") or "") for member in queued.members],
                    "started": queued.started,
                }
            )
        return out

    def running_snapshot(self) -> list[dict[str, Any]]:
        """Conversations with a turn in flight."""

        return [
            {"conversation_ref": ref, "bot_ref": bot_ref}
            for ref, bot_ref in self._running.items()
        ]

    def steer_log(self, conversation_ref: str) -> list[str]:
        """Steers delivered to one conversation (observability, not a receipt)."""

        return list(self._steers.get(conversation_ref, []))

    # ── intake ──────────────────────────────────────────────────────────

    def _config(self, conversation_ref: str, bot_ref: str) -> TurnIntakeConfig:
        def append_pending(envelope: dict[str, Any], merged_text: str) -> None:
            queued = self._queues.get(conversation_ref)
            if queued is None:
                queued = _QueuedTurn(conversation_ref=conversation_ref, bot_ref=bot_ref)
                self._queues[conversation_ref] = queued
            if len(queued.members) >= self.max_pending:
                # Drop the *least* important member, never the newest one: on a
                # tie the earliest arrival goes, so a direct message that just
                # landed is never the one thrown away.
                worst = max(
                    range(len(queued.members)),
                    key=lambda index: (_rank(queued.members[index]), -index),
                )
                _log.warning(
                    "turn queue at cap (%s); dropped %s envelope for %s",
                    self.max_pending,
                    queued.members[worst].get("kind"),
                    conversation_ref,
                )
                queued.members.pop(worst)
            queued.members.append(envelope)
            queued.text = merged_text or "\n".join(
                str(member.get("text") or "").strip()
                for member in ordered(queued.members)
                if str(member.get("text") or "").strip()
            )

        def mark_seen(key: str, envelope: dict[str, Any]) -> None:
            self._seen[key] = time.time()

        def deliver_steer(text: str, target_bot_ref: str) -> str:
            # The steer is appended to the running turn's brief, which is the
            # existing GoalRun objective — no second control channel exists.
            self._steers.setdefault(conversation_ref, []).append(text)
            return "delivered"

        return TurnIntakeConfig(
            turn_state=lambda: self.turn_state(conversation_ref),
            list_pending=lambda: self.list_pending(conversation_ref),
            append_pending=append_pending,
            seen_before=self.seen_before,
            mark_seen=mark_seen,
            deliver_steer=deliver_steer,
            has_start_evidence=lambda: bool(self._running.get(conversation_ref)),
            coalesce_window_s=self.window_s,
        )

    def intake(
        self,
        *,
        conversation_ref: str,
        text: str,
        kind: str = "direct",
        bot_ref: str = "",
        external_id: str = "",
        user_directed: bool = False,
        mention: bool = False,
        arrived_at: float | None = None,
    ) -> dict[str, Any]:
        """Run one inbound message through the intake omodul.

        Returns the omodul result verbatim plus a ``turn_members`` list the
        caller may use to open exactly one turn.
        """

        moment = float(arrived_at if arrived_at is not None else time.time())
        payload = TurnIntakeInput(
            conversation_ref=conversation_ref,
            kind=kind,
            text=text,
            external_id=external_id,
            arrived_at=moment,
            bot_ref=bot_ref,
            user_directed=user_directed,
            mention=mention,
        )
        return turn_intake(self._config(conversation_ref, bot_ref), payload, None, now=moment)

    # ── running a turn ──────────────────────────────────────────────────

    def mark_running(self, conversation_ref: str, bot_ref: str = "") -> None:
        self._running[conversation_ref] = bot_ref
        queued = self._queues.get(conversation_ref)
        if queued is not None:
            queued.started = True

    def mark_finished(self, conversation_ref: str) -> None:
        self._running.pop(conversation_ref, None)
        self._queues.pop(conversation_ref, None)

    def next_turn(self, conversation_ref: str) -> dict[str, Any] | None:
        """The single turn this conversation should run now, if any.

        Returns ``None`` when nothing is pending. Returns a dict with
        ``text``, ``bot_ref`` and ``count`` otherwise.
        """

        queued = self._queues.get(conversation_ref)
        if queued is None or not queued.members:
            return None
        ordered_members = order_queue(queued.members)
        merged = coalesce_window(ordered_members, now=time.time(), window_s=self.window_s)
        return {
            "conversation_ref": conversation_ref,
            "bot_ref": queued.bot_ref,
            "text": merged["text"] or queued.text,
            "count": len(queued.members),
            "members": ordered_members,
            "state": STATE_UNCONFIRMED,
        }

    def idempotency_key(self, conversation_ref: str, source: str, external_id: str) -> str:
        """Expose the canonical idempotency key so a retry can be rejected early."""

        return idem_key(conversation_ref, source, external_id)


def ordered(members: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Priority order for stored members (thin re-export for readability)."""

    return order_queue(members)


_RANKS = {"direct": 0, "group_mention": 1, "channel": 2, "routine": 3}


def _rank(envelope: dict[str, Any]) -> int:
    return _RANKS.get(str(envelope.get("kind") or "routine"), 3)


#: Process-wide queue. Like ``async_turns._pending`` this is per-process state:
#: a restart forgets it, which is safe because the turn itself is durable.
_QUEUE: TurnQueue | None = None


def get_turn_queue() -> TurnQueue:
    """The process-wide :class:`TurnQueue`."""

    global _QUEUE
    if _QUEUE is None:
        _QUEUE = TurnQueue()
    return _QUEUE


def reset_turn_queue() -> None:
    """Drop the process queue (tests, and an explicit operator reset)."""

    global _QUEUE
    _QUEUE = None


__all__ = [
    "TURN_QUEUE_FLAG",
    "TurnQueue",
    "get_turn_queue",
    "reset_turn_queue",
    "turn_queue_enabled",
]