"""Per-turn repeat-call guard (OpenMausBot loop-guard method, adapted).

Only observes and steers, never blocks: when the model makes the same
tool call with the same arguments for the fifth time in one turn, the
tool result carries a one-line steer note suggesting different
arguments, or a report of what is blocking it. Calls with no arguments
never count: an empty call carries no identity to repeat, and counting
it misfires on polling loops (the lesson of upstream's ``6b479913``
fix, where unnamed MCP calls were wrongly counted as repeats).

The detector lives in a context variable bound for one turn by
:func:`server.bot_team.turns.finish_bot_turn`; outside a turn there is
no detector and tool execution is untouched. Nested turns (a
delegation's target turn inside a source turn) bind a fresh detector
and restore the outer one on exit, by context-variable token.
"""

from __future__ import annotations

import contextvars
import json

#: Identical calls in one turn before the steer note appears.
STEER_AT = 5


class RepeatDetector:
    """Counts identical tool calls within one turn."""

    def __init__(self) -> None:
        self._counts: dict[str, int] = {}

    def observe(self, tool: str, arguments: dict) -> int:
        """Record one call; return its running count (0 when not counted)."""

        if not arguments:
            return 0
        key = str(tool) + "\0" + json.dumps(
            arguments, sort_keys=True, ensure_ascii=False, default=str
        )
        count = self._counts.get(key, 0) + 1
        self._counts[key] = count
        return count

    def count(self, tool: str, arguments: dict) -> int:
        """How many times this exact call has run (0 when never counted)."""

        if not arguments:
            return 0
        key = str(tool) + "\0" + json.dumps(
            arguments, sort_keys=True, ensure_ascii=False, default=str
        )
        return self._counts.get(key, 0)


def steer_note(tool: str, count: int) -> str | None:
    """The steer note for a repeated call, or ``None`` below the threshold."""

    if count < STEER_AT:
        return None
    return (
        f"[repeat guard: this is the {count}th identical call to {tool} "
        "with the same arguments this turn. Change the arguments, try a "
        "different tool, or stop and report what is blocking you.]"
    )


def attach_steer(result: str, tool: str, count: int) -> str:
    """Append the steer note to a tool result when the threshold is hit."""

    note = steer_note(tool, count)
    if note is None or not isinstance(result, str):
        return result
    return f"{result}\n{note}"


_detector_ctx: contextvars.ContextVar[RepeatDetector | None] = contextvars.ContextVar(
    "veya_repeat_detector", default=None
)


def bind_repeat_detector() -> contextvars.Token:
    """Bind a fresh detector for one turn; restore it with the token."""

    return _detector_ctx.set(RepeatDetector())


def reset_repeat_detector(token: contextvars.Token) -> None:
    """Restore the detector that was active before :func:`bind_repeat_detector`."""

    _detector_ctx.reset(token)


def current_repeat_detector() -> RepeatDetector | None:
    """The detector bound for the running turn, or ``None`` outside one."""

    return _detector_ctx.get()


__all__ = [
    "STEER_AT",
    "RepeatDetector",
    "attach_steer",
    "bind_repeat_detector",
    "current_repeat_detector",
    "reset_repeat_detector",
    "steer_note",
]
