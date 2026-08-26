"""Observer protocol with explicit fail-open/fail-closed policy."""

from __future__ import annotations

import inspect
from typing import Any, Protocol


class TurnObserver(Protocol):
    async def before_turn(self, ctx: Any) -> None: ...
    async def after_model(self, ctx: Any, response: Any) -> None: ...
    async def before_tool(self, ctx: Any, call: Any) -> None: ...
    async def after_tool(self, ctx: Any, result: Any) -> None: ...
    async def on_stop(self, ctx: Any, reason: str) -> None: ...


class ObserverRuntime:
    """Dispatch observers without allowing optional quality hooks to fail work."""

    def __init__(self, observers: list[TurnObserver] | None = None):
        self.observers = list(observers or [])

    async def notify(self, method: str, *args: Any, fail_closed: bool = False) -> None:
        for observer in self.observers:
            try:
                value = getattr(observer, method)(*args)
                if inspect.isawaitable(value):
                    await value
            except Exception:
                if fail_closed:
                    raise
