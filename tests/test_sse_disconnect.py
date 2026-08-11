"""SSE 生成器断开检测 + 协程/队列清理 (Phase 4 死锁硬化)。

覆盖 server.sse.SSEQueue.events 的三条保证:
1. 正常收尾 (None 哨兵) → 发 [DONE] 并从 _queues 清理会话队列。
2. 客户端断开 (request.is_disconnected() → True) → 心跳节拍上退出生成器,
   不再永久悬挂于裸 await get(), 并清理 _queues。
3. 未断开时心跳超时 → 发 ": ping" 保活注释行。
"""

from __future__ import annotations

import asyncio

import pytest

import server.sse as sse


class _FakeRequest:
    """最小 Request 替身: 只实现 events() 用到的 is_disconnected()。"""

    def __init__(self, disconnected: bool = False) -> None:
        self._disconnected = disconnected

    async def is_disconnected(self) -> bool:
        return self._disconnected


@pytest.fixture(autouse=True)
def _clean_registry():
    sse._queues.clear()
    yield
    sse._queues.clear()


@pytest.mark.asyncio
async def test_events_done_sentinel_cleans_up_queue():
    q = sse.get_or_create_queue("s-done")
    assert "s-done" in sse._queues
    q.on_step({"type": "text_delta", "delta": "hi"})
    q.close()  # None 哨兵

    frames = [frame async for frame in q.events(_FakeRequest())]

    assert any('"delta": "hi"' in f or '"delta":"hi"' in f for f in frames)
    assert frames[-1] == "data: [DONE]\n\n"
    # 消费结束 → 会话队列已清理 (防 _queues 无限增长)
    assert "s-done" not in sse._queues


@pytest.mark.asyncio
async def test_events_stops_on_client_disconnect(monkeypatch):
    # 心跳缩到近 0: 无数据时立即走超时分支 → 检测断开
    monkeypatch.setattr(sse, "_HEARTBEAT_S", 0.01)
    q = sse.get_or_create_queue("s-gone")
    req = _FakeRequest(disconnected=True)

    # 队列永不收到 None 哨兵: 旧实现会永久悬挂; 硬化后应在断开检测处返回
    gen = q.events(req)
    frames = await asyncio.wait_for(_collect(gen), timeout=2.0)

    # 断开 → 直接退出, 不产生 [DONE] 帧
    assert frames == []
    assert "s-gone" not in sse._queues


@pytest.mark.asyncio
async def test_events_heartbeat_when_connected(monkeypatch):
    monkeypatch.setattr(sse, "_HEARTBEAT_S", 0.01)
    q = sse.get_or_create_queue("s-alive")
    req = _FakeRequest(disconnected=False)

    gen = q.events(req)
    # 第一拍: 无数据 + 未断开 → 保活注释行
    first = await asyncio.wait_for(gen.__anext__(), timeout=2.0)
    assert first == ": ping\n\n"
    await gen.aclose()  # 触发 finally 清理
    assert "s-alive" not in sse._queues


async def _collect(gen) -> list[str]:
    return [frame async for frame in gen]
