"""Cancellation propagation for a task/delegate execution tree."""

from __future__ import annotations

import asyncio
from collections import defaultdict


class CancellationTree:
    def __init__(self):
        self._children: dict[str, set[str]] = defaultdict(set)
        self._events: dict[str, asyncio.Event] = {}

    def register(self, node_id: str, parent_id: str | None = None) -> asyncio.Event:
        event = self._events.setdefault(node_id, asyncio.Event())
        if parent_id:
            self._children[parent_id].add(node_id)
            if self.is_cancelled(parent_id):
                event.set()
        return event

    def cancel(self, node_id: str) -> list[str]:
        cancelled: list[str] = []

        def walk(current: str) -> None:
            event = self._events.setdefault(current, asyncio.Event())
            if not event.is_set():
                event.set()
                cancelled.append(current)
            for child in self._children.get(current, ()):
                walk(child)

        walk(node_id)
        return cancelled

    def is_cancelled(self, node_id: str) -> bool:
        event = self._events.get(node_id)
        return bool(event and event.is_set())
