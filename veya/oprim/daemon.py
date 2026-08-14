"""veya/oprim/daemon — daemon 生命周期原子操作（物理触手，Human-in-the-loop）。

阶段 3 原子元素：oprim_pause_daemon / oprim_resume_daemon / oprim_daemon_status /
oprim_daemon_bind（daemon 引擎注册处理器用）。

规则：
- 经注入的 DaemonBus 句柄（默认 container 全局句柄）；
- pause/resume 是请求-响应 RPC（等 daemon 确认），status 只读查询；
- 本层不含挂起策略（何时挂起由阶段 4 agent_loop / 人类注入决定）。
"""

from __future__ import annotations

from typing import Any

_TOPIC_PAUSE = "daemon.pause"
_TOPIC_RESUME = "daemon.resume"
_TOPIC_STATUS = "daemon.status"


def _bus_of(bus: Any) -> Any:
    if bus is not None:
        return bus
    from veya.obase.container import get_bus

    return get_bus()


async def daemon_pause(bus: Any = None, *, session_id: str = "", reason: str = "", timeout: float = 30.0) -> dict:
    """请求 daemon 挂起任务（Human-in-the-loop 等待人类输入）。"""
    return await _bus_of(bus).request(  # type: ignore[attr-defined]
        _TOPIC_PAUSE, {"session_id": session_id, "reason": reason}, timeout=timeout
    )


async def daemon_resume(bus: Any = None, *, session_id: str = "", timeout: float = 30.0) -> dict:
    """请求 daemon 恢复任务。"""
    return await _bus_of(bus).request(  # type: ignore[attr-defined]
        _TOPIC_RESUME, {"session_id": session_id}, timeout=timeout
    )


async def daemon_status(bus: Any = None, *, session_id: str = "", timeout: float = 10.0) -> dict:
    """查询任务状态（running/paused/completed/failed）。"""
    return await _bus_of(bus).request(  # type: ignore[attr-defined]
        _TOPIC_STATUS, {"session_id": session_id}, timeout=timeout
    )


async def daemon_bind(bus: Any = None, topic: str = "", handler: Any = None) -> None:
    """daemon 引擎侧：注册 pause/resume/status 处理器（handler: async (payload) -> dict）。"""
    topic = topic or _TOPIC_PAUSE
    if handler is None:
        raise ValueError("handler 不能为空")
    await _bus_of(bus).register_handler(topic, handler)  # type: ignore[attr-defined]


__all__ = ["daemon_bind", "daemon_pause", "daemon_resume", "daemon_status"]
