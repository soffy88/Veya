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

import contextlib
import contextvars
import time
from collections.abc import Callable
from typing import Any

_on_step_ctx: contextvars.ContextVar[Callable | None] = contextvars.ContextVar(
    "on_step", default=None
)


def fire_step(event: dict[str, Any]) -> None:
    """Fire an on_step event to the current context's callback (if any)."""
    cb = _on_step_ctx.get()
    if cb is not None:
        with contextlib.suppress(Exception):
            cb(event)  # on_step errors must never abort the main flow


def _to_envelope(event: dict[str, Any]) -> dict[str, Any]:
    """裸 dict → 纯增量信封 (对标"Pi"清单 P1: 稳定消息结构)。

    只新增 topic/payload/ts/trace_id 四个字段, 原字段一个不删不改——前端 4 套
    解析器仍读老的扁平字段, 新信封字段是给未来 UI/SDK/审计用的额外信息, 不是
    替换。若原 dict 已占用某个信封键名 (如 zero_trust_vault.py 的 vault_hitl
    自带 "payload" 子结构), 保留原字段, 跳过那一个信封键。任何异常退化为原样
    返回, 绝不能因为信封逻辑本身拖垮 SSE 流。
    """
    try:
        if not isinstance(event, dict):
            return event
        envelope = dict(event)
        envelope.setdefault("topic", str(event.get("type") or event.get("event") or "unknown"))
        if "payload" not in envelope:
            envelope["payload"] = {k: v for k, v in event.items() if k not in ("type", "event")}
        envelope.setdefault("ts", time.time())
        envelope.setdefault(
            "trace_id",
            str(
                event.get("session_id")
                or event.get("sid")
                or event.get("task_id")
                or event.get("plan_id")
                or event.get("request_id")
                or ""
            ),
        )
        return envelope
    except Exception:
        return event
