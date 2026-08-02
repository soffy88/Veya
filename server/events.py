"""
server/events.py — on_step ContextVar

Holds the per-request on_step callback via asyncio ContextVar.
Using ContextVar avoids threading the callback through every function signature
while remaining coroutine-safe (each async task has its own context copy).

Usage:
    # Set in coordinator.handle():
    token = _on_step_ctx.set(callback)
    try: ...
    finally: _on_step_ctx.reset(token)

    # Fire from anywhere in the call chain:
    from server.events import fire_step
    fire_step({"type": "tool_call", "tool_name": "write"})
"""
from __future__ import annotations

import contextvars
from typing import Callable, Any

_on_step_ctx: contextvars.ContextVar[Callable | None] = contextvars.ContextVar(
    "on_step", default=None
)


def fire_step(event: dict[str, Any]) -> None:
    """Fire an on_step event to the current context's callback (if any)."""
    cb = _on_step_ctx.get()
    if cb is not None:
        try:
            cb(event)
        except Exception:
            pass  # on_step errors must never abort the main flow
