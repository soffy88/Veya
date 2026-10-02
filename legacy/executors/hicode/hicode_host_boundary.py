"""Host-side lifecycle primitives for the Hicode process boundary.

This module deliberately contains no 3O imports.  The Veya control plane only
needs a small async gate to serialize workspace snapshots and the independent
Hicode serve session; 3O runtime construction and package imports belong to
the managed Python companion.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path


class HicodeHostGate:
    """Process-local lifecycle gate; it is not a 3O runtime or worker engine."""

    def __init__(self) -> None:
        self._workspace_guard = asyncio.Lock()
        self._workspace_locks: dict[str, asyncio.Lock] = {}
        self._slot_guard = asyncio.Lock()
        self._slots: dict[str, asyncio.Lock] = {}
        self._user_slots: dict[tuple[str, str], asyncio.Lock] = {}

    @staticmethod
    def _workspace_key(workspace: str | None) -> str:
        if not workspace:
            return "_default"
        return str(Path(workspace).expanduser().resolve())

    async def _workspace_lock(self, workspace: str | None) -> asyncio.Lock:
        key = self._workspace_key(workspace)
        async with self._workspace_guard:
            return self._workspace_locks.setdefault(key, asyncio.Lock())

    async def _slot_lock(
        self, kind: str, owner_id: str = ""
    ) -> tuple[asyncio.Lock, asyncio.Lock | None]:
        async with self._slot_guard:
            slot = self._slots.setdefault(kind, asyncio.Lock())
            user = (
                self._user_slots.setdefault((kind, owner_id), asyncio.Lock()) if owner_id else None
            )
        return slot, user

    @asynccontextmanager
    async def async_workspace(self, workspace: str | None) -> AsyncIterator[str]:
        key = self._workspace_key(workspace)
        lock = await self._workspace_lock(workspace)
        await lock.acquire()
        try:
            yield key
        finally:
            lock.release()

    @asynccontextmanager
    async def async_slot(self, kind: str, owner_id: str = "") -> AsyncIterator[str]:
        slot, user = await self._slot_lock(kind, owner_id)
        await slot.acquire()
        if user is not None:
            await user.acquire()
        try:
            yield kind
        finally:
            if user is not None:
                user.release()
            slot.release()


_HICODE_HOST_GATE = HicodeHostGate()


def hicode_host_gate() -> HicodeHostGate:
    """Return the singleton host lifecycle gate."""

    return _HICODE_HOST_GATE


__all__ = ["HicodeHostGate", "hicode_host_gate"]
