"""Per-bot turn mutex (OpenMausBot room-member lock method, adapted).

One bot runs one turn at a time, across conversations: two rooms (or a
room and a direct message) racing the same bot serialize instead of
interleaving model calls and replies. A turn that delegates to another
bot, and the target turn that runs inline in the same task, re-enters
the same bot's lock by task identity — cross-bot delegation is
inline-awaited, so a chain A -> B -> A never deadlocks on its own
lock. A genuinely concurrent turn for the same bot, from another
task, waits for the lock; it is never refused.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator


class _BotTurnLock:
    __slots__ = ("lock", "loop", "owner", "depth")

    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.loop: asyncio.AbstractEventLoop | None = None
        self.owner: asyncio.Task[object] | None = None
        self.depth = 0


_LOCKS: dict[str, _BotTurnLock] = {}


@contextlib.asynccontextmanager
async def bot_turn_lock(bot_id: str) -> AsyncIterator[None]:
    """Hold *bot_id*'s turn lock; re-entrant within one task."""

    entry = _LOCKS.setdefault(bot_id, _BotTurnLock())
    task = asyncio.current_task()
    if task is None:
        yield
        return
    # An asyncio.Lock binds to the loop that first contends it. A lock
    # carried over from a dead loop (a fresh loop per test, or a loop
    # restart) would refuse every acquire, so an entry owned by another
    # loop is rebuilt: its owner task died with that loop. One running
    # loop at a time is assumed, which holds in the server process.
    loop = asyncio.get_running_loop()
    if entry.loop is not loop:
        entry.lock = asyncio.Lock()
        entry.loop = loop
        entry.owner = None
        entry.depth = 0
    if entry.owner is task:
        entry.depth += 1
        try:
            yield
        finally:
            entry.depth -= 1
        return
    await entry.lock.acquire()
    entry.owner = task
    entry.depth = 1
    try:
        yield
    finally:
        entry.depth = 0
        entry.owner = None
        entry.lock.release()


__all__ = ["bot_turn_lock"]
