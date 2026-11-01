"""Stall watchdog: one outer deadline per turn (OpenMausBot room-turn-timeout method, adapted).

A turn that makes no progress before the deadline is recorded as
``stalled`` — not as a provider error — and is never retried: the
conversation gets a short Chinese note saying so. The deadline covers
the whole turn, including waiting for the per-bot lock, because a turn
stuck behind a wedged holder is stalled too. Progress events are not
tracked yet; the deadline is wall-clock only.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from collections.abc import AsyncIterator

#: Wall-clock seconds for one turn before it is declared stalled.
DEFAULT_TURN_TIMEOUT_S = 600.0


def turn_timeout_s() -> float | None:
    """The turn deadline in seconds, or ``None`` when disabled.

    ``VEYA_TURN_TIMEOUT_S`` overrides the default; a value of zero or
    less disables the deadline. An unparseable value keeps the default.
    """

    raw = os.environ.get("VEYA_TURN_TIMEOUT_S", "").strip()
    if not raw:
        return DEFAULT_TURN_TIMEOUT_S
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_TURN_TIMEOUT_S
    return value if value > 0 else None


def stall_note(timeout_s: float) -> str:
    """The Chinese note a stalled turn leaves in the conversation."""

    return (
        f"（本轮等待超过 {timeout_s:g} 秒仍无进展，已按 stalled 停下，"
        "没有重试。如需继续，请再发一次。）"
    )


@contextlib.asynccontextmanager
async def turn_deadline() -> AsyncIterator[asyncio.Timeout | None]:
    """Bound one turn by :func:`turn_timeout_s`; yield the scope or ``None``."""

    timeout_s = turn_timeout_s()
    if timeout_s is None:
        yield None
        return
    async with asyncio.timeout(timeout_s) as scope:
        yield scope


__all__ = [
    "DEFAULT_TURN_TIMEOUT_S",
    "stall_note",
    "turn_deadline",
    "turn_timeout_s",
]
