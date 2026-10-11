"""Deliver a mid-turn steer to the running turn (W11-E S4).

A correction typed while a bot turn is running is recorded by the turn queue
(:mod:`server.nano_w10.turn_queue`) as a **steer** — the same path an inbound
direct message takes, so there is no second control channel. This module is the
one place that hands that steer to the running turn: the next model call sees
it as a ``user`` message, so the following output follows the correction.

The injection is the same shape as :func:`server.bot_team.memory.inject_memory_context`
— it edits the semantic history the turn already holds, immediately before the
model call, and never opens an execution path of its own.
"""

from __future__ import annotations

from typing import Any

__all__ = ["deliver_steers_to_turn"]


def deliver_steers_to_turn(semantic_agent: Any, session_id: str, bot_id: str) -> list[str]:
    """Append any pending steer for ``bot_id``'s running turn to its history.

    Called from the GoalRun semantic step, right before the model call. Drains
    the queue (a steer is injected exactly once) and appends each as a
    ``{"role": "user"}`` message, so the model's next answer is made with the
    correction in view.

    Returns the steers that were delivered (empty when the queue is off, the
    bot has no steer, or the agent exposes no semantic history).
    """

    from server.nano_w10.turn_queue import get_turn_queue, turn_queue_enabled

    if not turn_queue_enabled():
        return []
    steers = get_turn_queue().drain_steers(str(bot_id or ""))
    if not steers:
        return []
    histories = getattr(semantic_agent, "_histories", None)
    if not isinstance(histories, dict):
        return []
    messages = histories.get(session_id)
    if messages is None:
        messages = [{"role": "system", "content": semantic_agent.get_system_prompt()}]
        histories[session_id] = messages
    for text in steers:
        body = str(text or "").strip()
        if body:
            messages.append({"role": "user", "content": body})
    return [str(text) for text in steers if str(text or "").strip()]
