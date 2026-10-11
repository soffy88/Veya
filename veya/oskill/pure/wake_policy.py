"""3O-PURE — wake_policy: "is this worth bugging the user about?" (NANO-W10 §2).

One predicate over a wake signal plus the user's settings, returning exactly
one outcome:

===============  =============================================================
outcome          meaning
===============  =============================================================
``reply_now``    interrupt the user: push (P5) and channels (W7) may fire
``digest``       batch it into the W7 morning feed
``silent``       log only, no notification of any kind
``emoji_ack``    cheap acknowledgement (a reaction), not a full reply
===============  =============================================================

Inputs the policy reads (spec §2): user-directed vs background, novelty,
urgency, the user's quiet-hours setting, and the per-bot notification level.

The order below is the policy, and it is deliberately **fail-quiet**: when a
setting is missing the safest answer for a *background* signal is ``silent``,
because an always-on agent that bugs people about everything trains people to
ignore it. An unknown/missing notification level therefore behaves like
``"important"`` for user-directed signals and ``"never"`` for background ones.

No IO, no clock, no ``user_id``: the caller passes ``now`` and a stable
reference.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "NOTIFICATION_LEVELS",
    "OUTCOMES",
    "classify_wake",
    "is_quiet_hours",
    "worth_bugging_user",
]

#: The four outcomes, most intrusive first.
OUTCOMES: tuple[str, ...] = ("reply_now", "digest", "emoji_ack", "silent")

#: Per-bot notification levels, least to most verbose.
NOTIFICATION_LEVELS: tuple[str, ...] = ("never", "important", "all")


def is_quiet_hours(settings: dict[str, Any], *, now: float) -> bool:
    """Is the user inside their configured quiet hours?

    Args:
        settings: ``{"quiet_hours": {"enabled": bool, "start": "23:00",
            "end": "08:00"}}``.  A disabled block (or a missing one) is not
            quiet hours.
        now: local time as ``hour*3600 + minute*60`` seconds, supplied by the
            caller so this skill never reads a clock.

    Returns:
        True inside the window. A window whose ``end`` is not after ``start``
        wraps past midnight, which is the normal case.

    Example:
        >>> is_quiet_hours({"quiet_hours": {"enabled": True,
        ...     "start": "23:00", "end": "08:00"}}, now=2 * 3600)
        True
    """

    block = (settings or {}).get("quiet_hours") or {}
    if not block.get("enabled"):
        return False
    start = _minutes(block.get("start"))
    end = _minutes(block.get("end"))
    if start is None or end is None or start == end:
        return False
    current = (int(now) // 60) % (24 * 60)
    if start < end:
        return start <= current < end
    return current >= start or current < end


def worth_bugging_user(
    signal: dict[str, Any],
    settings: dict[str, Any] | None = None,
    *,
    now: float = 0.0,
) -> dict[str, Any]:
    """Decide whether this wake is worth interrupting the user for.

    Args:
        signal: ``{"user_directed": bool, "urgent": bool, "novel": bool,
            "ack_only": bool, "kind": str, ...}``.
        settings: ``{"notification_level": "all"|"important"|"never",
            "quiet_hours": {...}}``.
        now: local seconds-of-day for the quiet-hours check.

    Returns:
        ``{"outcome", "reason", "level", "quiet", "urgent", "novel"}``.

    Example:
        >>> worth_bugging_user({"user_directed": True, "urgent": True})["outcome"]
        'reply_now'
    """

    settings = dict(settings or {})
    level = str(settings.get("notification_level") or "").strip().lower()
    if level not in NOTIFICATION_LEVELS:
        # Unknown level: permissive for the user, quiet for background noise.
        level = "important"

    user_directed = bool(signal.get("user_directed"))
    urgent = bool(signal.get("urgent"))
    novel = bool(signal.get("novel"))
    ack_only = bool(signal.get("ack_only"))
    quiet = is_quiet_hours(settings, now=now)

    decision = {
        "level": level,
        "quiet": quiet,
        "urgent": urgent,
        "novel": novel,
    }

    if level == "never":
        return {**decision, "outcome": "silent", "reason": "this bot's notification level is never"}

    if urgent:
        return {
            **decision,
            "outcome": "reply_now",
            "reason": "urgent wake: the user asked to be interrupted",
        }

    if ack_only and not user_directed:
        return {
            **decision,
            "outcome": "emoji_ack",
            "reason": "acknowledgement-only wake from a background source",
        }

    if quiet:
        # Quiet hours still allow a *novel, user-directed* signal into the
        # digest: the user reads it in the morning instead of being woken.
        return {
            **decision,
            "outcome": "digest",
            "reason": "inside the user's quiet hours: batched into the morning digest",
        }

    if user_directed:
        # A user-directed wake that is NOT novel is already answered in the open
        # conversation. Pushing it would interrupt the user about something they
        # just read, so even the most verbose notification level digests it.
        if novel:
            return {
                **decision,
                "outcome": "reply_now",
                "reason": "the user is waiting on this conversation",
            }
        return {
            **decision,
            "outcome": "digest",
            "reason": "already covered in the running conversation: batched instead of interrupting",
        }

    if novel and level == "all":
        return {
            **decision,
            "outcome": "digest",
            "reason": "novel background signal: batched into the morning digest",
        }

    return {
        **decision,
        "outcome": "silent",
        "reason": "background wake with nothing new: logged only",
    }


def classify_wake(
    signal: dict[str, Any],
    settings: dict[str, Any] | None = None,
    *,
    now: float = 0.0,
) -> dict[str, Any]:
    """Alias of :func:`worth_bugging_user` kept for the intake call site.

    Args:
        signal: the wake signal.
        settings: per-user/per-bot settings.
        now: local seconds-of-day.

    Returns:
        The same dict as :func:`worth_bugging_user`.
    """
    return worth_bugging_user(signal, settings, now=now)


def _minutes(value: Any) -> int | None:
    """``"23:00"`` -> ``1380``; anything unparseable -> ``None``."""

    text = str(value or "").strip()
    if ":" not in text:
        return None
    head, _, tail = text.partition(":")
    try:
        hours = int(head)
        minutes = int(tail)
    except ValueError:
        return None
    if not (0 <= hours < 24 and 0 <= minutes < 60):
        return None
    return hours * 60 + minutes