"""GET /api/v1/agent/stream_status — 断流重连前的后台任务探活。

前端网络抖动后要重连 GET /stream/{sid} 续接事件, 但后台任务已经跑完/取消
时重连只会挂在一个空队列上永远等不到新事件 — 这个端点让前端先判断"后台
任务还在跑吗", 不在就别重连, 直接退化成发新消息。
"""

from __future__ import annotations

import asyncio

import pytest

from server.routes.legacy_agent import legacy_agent_stream_status


@pytest.mark.asyncio
async def test_stream_status_active_for_running_task(monkeypatch):
    from server import coordinator_master

    async def _forever():
        await asyncio.sleep(3600)

    task = asyncio.create_task(_forever())
    monkeypatch.setattr(coordinator_master, "_active_streams", {"sid-1": task})
    try:
        result = await legacy_agent_stream_status("sid-1")
        assert result == {"active": True}
    finally:
        task.cancel()


@pytest.mark.asyncio
async def test_stream_status_inactive_for_finished_task(monkeypatch):
    from server import coordinator_master

    async def _noop():
        return "done"

    task = asyncio.create_task(_noop())
    await task  # 让任务先跑完 (task.done() == True)
    monkeypatch.setattr(coordinator_master, "_active_streams", {"sid-2": task})

    result = await legacy_agent_stream_status("sid-2")
    assert result == {"active": False}


@pytest.mark.asyncio
async def test_stream_status_inactive_for_unknown_session(monkeypatch):
    from server import coordinator_master

    monkeypatch.setattr(coordinator_master, "_active_streams", {})
    result = await legacy_agent_stream_status("no-such-sid")
    assert result == {"active": False}
