"""Runner operations oskill — when may a runner restart? (NANO-W8).

One pure decision, over a value: :func:`runner_safe_to_restart` answers
whether a runner's current state allows a self-update to hand off to a new
version.

Restarting a runner mid-turn interrupts a bot that is working.  The rule is
therefore about *work in flight*, not about the update being convenient: no
active turn and nothing queued means safe, anything else means not now — with
the reason naming what is in the way so the caller can retry rather than
force.

No IO and no LLM: the runner state is handed in, the answer comes back.
"""

from __future__ import annotations

from typing import Any

#: Keys the answer understands, so a caller with a thinner state object still
#: gets a decision instead of a KeyError.
ACTIVE_TURNS_KEYS = ("active_turns", "turns", "running_turns")
PENDING_KEYS = ("pending_turns", "queued_turns")


def runner_safe_to_restart(runner_state: dict[str, Any]) -> dict[str, Any]:
    """Whether this runner may restart right now.

    Args:
        runner_state: ``{"active_turns": [...], "pending_turns": 0}``. Only
            "is there work in flight" is read; bot names are never echoed
            back.

    Returns:
        ``{"safe": bool, "reason": str, "active_turns": int,
        "pending_turns": int}``
    """

    state = dict(runner_state or {})
    active = _first_list(state, ACTIVE_TURNS_KEYS)
    pending = _first_count(state, PENDING_KEYS)

    if active:
        return {
            "safe": False,
            "reason": f"{len(active)} active bot turn(s) are running on this runner",
            "active_turns": len(active),
            "pending_turns": pending,
        }
    if pending:
        return {
            "safe": False,
            "reason": f"{pending} bot turn(s) are queued and would be interrupted",
            "active_turns": 0,
            "pending_turns": pending,
        }
    return {
        "safe": True,
        "reason": "no active turns",
        "active_turns": 0,
        "pending_turns": pending,
    }


def _first_list(state: dict[str, Any], keys: tuple[str, ...]) -> list[Any]:
    for key in keys:
        value = state.get(key)
        if isinstance(value, (list, tuple, set)):
            return list(value)
    return []


def _first_count(state: dict[str, Any], keys: tuple[str, ...]) -> int:
    for key in keys:
        value = state.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            return max(value, 0)
        if isinstance(value, (list, tuple, set)):
            return len(value)
    return 0


__all__ = ["runner_safe_to_restart"]
