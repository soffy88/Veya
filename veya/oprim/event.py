"""veya/oprim/event — 事件发射原子操作（物理触手）。

阶段 3 原子元素：oprim_emit_event。

规则：
- 经注入的 EventBarrier 句柄（默认 container 全局句柄）；
- 本层只负责构造标准 Event 并发出；订阅/屏障是 EventBarrier 的职责；
- 原子性：一次 emit = 一条标准事件。
"""

from __future__ import annotations

from typing import Any

from veya.obase.interfaces import Event


def _barrier_of(barrier: Any) -> Any:
    if barrier is not None:
        return barrier
    from veya.obase.container import get_barrier

    return get_barrier()


def emit_event(topic: str, payload: dict[str, Any] | None = None, barrier: Any = None, trace_id: str = "") -> Event:
    """发出标准事件（同步、无阻塞）。返回构造的 Event。"""
    event = Event(topic=topic, payload=payload or {}, trace_id=trace_id)
    _barrier_of(barrier).emit(event)
    return event


__all__ = ["emit_event"]
