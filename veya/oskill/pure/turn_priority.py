"""3O-PURE — turn_priority: ordering, coalescing and steer decisions (NANO-W10 §1).

Four pure rules over inbound turn envelopes. No IO, no clock, no `user_id`:
the caller passes ``now`` and a stable conversation reference, so the same
inputs always give the same decision in a test, in a GoalRun step and in a
report.

Rules
-----
:classify_inbound`
    → ``direct`` | ``group_mention`` | ``channel`` | ``routine``.
    Priority is the index in :data:`PRIORITY_ORDER`; lower runs first.
:func:`order_queue`
    stable sort by (priority, arrival) — arrival order breaks ties so a
    routine never jumps ahead of a user message.
:func:`steer_decision`
    a **direct** message that arrives while a turn is *running* becomes a steer
    rather than a second turn. The returned receipt state reuses the W6 K
    states verbatim (``delivered`` / ``started`` / ``unconfirmed``) — delivery is
    never assumed.
:func:`coalesce_window`
    messages that land within ``window_s`` of the first message of a pending
    turn merge into one turn, keeping the highest priority and the concatenated
    body.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "INBOUND_KINDS",
    "PRIORITY_ORDER",
    "classify_inbound",
    "coalesce_window",
    "envelope_priority",
    "order_queue",
    "steer_decision",
    "STATE_DELIVERED",
    "STATE_STARTED",
    "STATE_UNCONFIRMED",
]

#: Inbound kinds, most important first. The order *is* the priority policy:
#: a direct user message outranks a mention, which outranks channel traffic,
#: which outranks routine/background work.
INBOUND_KINDS: tuple[str, ...] = ("direct", "group_mention", "channel", "routine")

PRIORITY_ORDER: dict[str, int] = {kind: index for index, kind in enumerate(INBOUND_KINDS)}

# W6 K delivery receipts, re-used verbatim so no second vocabulary appears.
STATE_DELIVERED = "delivered"
STATE_STARTED = "started"
STATE_UNCONFIRMED = "unconfirmed"

_DEFAULT_WINDOW_S = 2.0


def classify_inbound(envelope: dict[str, Any]) -> str:
    """Which priority class does this envelope belong to?

    Args:
        envelope: ``{"kind", "user_directed", "mention", ...}``.

    Returns:
        One of :data:`INBOUND_KINDS`. An unrecognised or missing kind is
        ``"routine"`` — the lowest rung — so an unknown sender can never
        outrank a real user message.

    Example:
        >>> classify_inbound({"kind": "direct", "user_directed": True})
        'direct'
    """

    kind = str(envelope.get("kind") or "").strip().lower()
    if kind in PRIORITY_ORDER:
        return kind
    if envelope.get("user_directed"):
        return "direct"
    if envelope.get("mention"):
        return "group_mention"
    return "routine"


def envelope_priority(envelope: dict[str, Any]) -> int:
    """Numeric priority of one envelope (lower runs first).

    Args:
        envelope: the inbound envelope.

    Returns:
        ``0`` for ``direct`` … ``3`` for ``routine``.

    Example:
        >>> envelope_priority({"kind": "routine"})
        3
    """

    return PRIORITY_ORDER[classify_inbound(envelope)]


def order_queue(envelopes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Order pending envelopes best-first, arrival breaking ties.

    Args:
        envelopes: pending envelopes, each with ``arrived_at`` (float).

    Returns:
        A new list; the input is not mutated.

    Example:
        >>> [e["kind"] for e in order_queue(
        ...     [{"kind": "routine", "arrived_at": 1.0},
        ...      {"kind": "direct", "arrived_at": 2.0}])]
        ['direct', 'routine']
    """

    return sorted(
        list(envelopes or []),
        key=lambda env: (envelope_priority(env), float(env.get("arrived_at") or 0.0)),
    )


def steer_decision(
    envelope: dict[str, Any],
    *,
    turn_state: str,
    has_start_evidence: bool = False,
) -> dict[str, Any]:
    """Should this message steer the running turn, queue behind it, or start one?

    A direct user message never waits for a slow turn to finish: it is
    delivered as a steer at the next safe point. Everything else queues in
    priority order.

    Args:
        envelope: the inbound envelope.
        turn_state: ``"none"`` (no turn in flight), ``"pending"`` (queued, not
            started) or ``"running"``.
        has_start_evidence: the model produced a first token / tool call for
            the running turn.

    Returns:
        ``{"action": "start"|"queue"|"steer", "priority": int, "kind": str,
        "state": <W6 K receipt state>, "reason": str}``. ``state`` is only
        meaningful for ``action == "steer"``.

    Example:
        >>> steer_decision({"kind": "direct"}, turn_state="running")["action"]
        'steer'
    """

    kind = classify_inbound(envelope)
    priority = PRIORITY_ORDER[kind]
    state = str(turn_state or "none").strip().lower()

    if state == "running" and kind == "direct":
        if has_start_evidence:
            receipt = STATE_STARTED
        elif state == "pending":
            receipt = STATE_DELIVERED
        else:
            receipt = STATE_UNCONFIRMED
        return {
            "action": "steer",
            "priority": priority,
            "kind": kind,
            "state": receipt,
            "reason": (
                "a direct user message steers the running turn at the next safe "
                "point instead of waiting for it to finish"
            ),
        }

    if state == "running":
        return {
            "action": "queue",
            "priority": priority,
            "kind": kind,
            "state": STATE_UNCONFIRMED,
            "reason": "a turn is already running; lower-priority traffic queues behind it",
        }

    return {
        "action": "queue",
        "priority": priority,
        "kind": kind,
        "state": STATE_UNCONFIRMED,
        "reason": "no turn is running; the message opens one",
    }


def coalesce_window(
    pending: list[dict[str, Any]],
    *,
    now: float,
    window_s: float | None = None,
) -> dict[str, Any]:
    """Merge envelopes that arrived inside one short window into a single turn.

    Args:
        pending: envelopes that arrived while a turn is queued but not yet
            started.
        now: the current time (caller-supplied; this skill never reads a clock).
        window_s: merge window in seconds, default :data:`_DEFAULT_WINDOW_S`.

    Returns:
        ``{"coalesced": bool, "count": int, "kind": <highest priority kind>,
        "priority": int, "text": <joined body>, "members": [envelope...],
        "first_arrived_at": float, "reason": str}``. ``count`` is 1 and
        ``coalesced`` is False when the window has already elapsed or the list
        is empty.

    Example:
        >>> out = coalesce_window(
        ...     [{"kind": "direct", "text": "hi", "arrived_at": 100.0}],
        ...     now=100.5)
        >>> out["coalesced"], out["count"]
        (True, 1)
    """

    window = _DEFAULT_WINDOW_S if window_s is None else float(window_s)
    items = list(pending or [])
    if not items:
        return {
            "coalesced": False,
            "count": 0,
            "kind": "",
            "priority": len(INBOUND_KINDS),
            "text": "",
            "members": [],
            "first_arrived_at": 0.0,
            "reason": "nothing pending",
        }

    if window <= 0.0:
        return {
            "coalesced": False,
            "count": len(items),
            "kind": classify_inbound(items[0]),
            "priority": envelope_priority(items[0]),
            "text": "",
            "members": items,
            "first_arrived_at": min(float(item.get("arrived_at") or 0.0) for item in items),
            "reason": "coalescing is disabled (window is 0s)",
        }

    first = min(float(item.get("arrived_at") or 0.0) for item in items)
    elapsed = float(now) - first
    if elapsed > window:
        return {
            "coalesced": False,
            "count": len(items),
            "kind": classify_inbound(items[0]),
            "priority": envelope_priority(items[0]),
            "text": "",
            "members": items,
            "first_arrived_at": first,
            "reason": (
                f"{elapsed:.3f}s since the first message is outside the "
                f"{window}s coalescing window"
            ),
        }

    ordered = order_queue(items)
    bodies = [str(item.get("text") or "").strip() for item in ordered]
    texts = [body for body in bodies if body]
    return {
        "coalesced": True,
        "count": len(items),
        "kind": classify_inbound(ordered[0]),
        "priority": envelope_priority(ordered[0]),
        "text": "\n".join(texts),
        "members": ordered,
        "first_arrived_at": first,
        "reason": (
            f"{len(items)} message(s) arrived within {window}s of the first; "
            "merged into one turn keeping the highest priority"
        ),
    }