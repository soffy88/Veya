"""Harness steering (对标"Pi"清单 P1 Harness 生命周期)——运行中注入 follow-up 消息。

设计: steering 请求不单开 SSE 流 (绕开 SSEQueue 单消费者问题), 只是把消息排进
正在跑的那一轮; `_SteeringLongTaskDriver` 组合进主库 `chat_stream` 轮次循环里
唯一"每轮必经"的宿主注入点 (`long_task.pre_round`/`post_round`), 跟已有的长程
任务驱动器 (若有) 各管各的字段、互不冲突。只在"确认这个 session 当前真的有
一轮在跑"时才接受入队, 否则拒绝——防止消息在没有轮次消费的情况下无限期挂着。
"""

from __future__ import annotations

import asyncio

import pytest

from server.coordinator_master import (
    MasterCoordinator,
    _drain_steering_messages,
    _enqueue_steering_message,
    _session_turn_in_flight,
    _SteeringLongTaskDriver,
    _acquire_session_lock,
    _release_session_lock,
)
from server.events import _on_step_ctx


@pytest.mark.asyncio
async def test_session_turn_in_flight_false_when_no_lock_recorded():
    assert _session_turn_in_flight("never-seen-sid") is False


@pytest.mark.asyncio
async def test_session_turn_in_flight_true_while_locked_false_after_release():
    sid = "s-lock-1"
    lock = await _acquire_session_lock(sid)
    try:
        assert _session_turn_in_flight(sid) is True
    finally:
        _release_session_lock(sid, lock)
    assert _session_turn_in_flight(sid) is False


@pytest.mark.asyncio
async def test_enqueue_rejected_when_no_turn_in_flight():
    assert _enqueue_steering_message("s-idle", "hello") is False


@pytest.mark.asyncio
async def test_enqueue_accepted_while_turn_in_flight():
    sid = "s-lock-2"
    lock = await _acquire_session_lock(sid)
    try:
        assert _enqueue_steering_message(sid, "steer me") is True
        assert _drain_steering_messages(sid) == ["steer me"]
    finally:
        _release_session_lock(sid, lock)


@pytest.mark.asyncio
async def test_enqueue_rejects_once_queue_is_full():
    sid = "s-lock-3"
    lock = await _acquire_session_lock(sid)
    try:
        for i in range(20):
            assert _enqueue_steering_message(sid, f"msg-{i}") is True
        assert _enqueue_steering_message(sid, "one-too-many") is False
    finally:
        _release_session_lock(sid, lock)
        _drain_steering_messages(sid)


@pytest.mark.asyncio
async def test_drain_is_pop_not_peek():
    sid = "s-lock-4"
    lock = await _acquire_session_lock(sid)
    try:
        _enqueue_steering_message(sid, "a")
        assert _drain_steering_messages(sid) == ["a"]
        assert _drain_steering_messages(sid) == []  # 第二次 drain 应该是空
    finally:
        _release_session_lock(sid, lock)


class _FakeAgent:
    def __init__(self, histories: dict) -> None:
        self._histories = histories


class _FakeInnerDriver:
    def __init__(self) -> None:
        self.pre_round_called = False
        self.post_round_called_with = None

    async def pre_round(self):
        self.pre_round_called = True
        return "inner-round-context"

    async def post_round(self, outcome):
        self.post_round_called_with = outcome


@pytest.mark.asyncio
async def test_pre_round_injects_pending_messages_and_fires_event():
    sid = "s-driver-1"
    lock = await _acquire_session_lock(sid)
    try:
        _enqueue_steering_message(sid, "follow up 1")
        _enqueue_steering_message(sid, "follow up 2")

        agent = _FakeAgent({sid: [{"role": "system", "content": "sys"}]})
        driver = _SteeringLongTaskDriver(sid, agent, None)

        events: list[dict] = []
        token = _on_step_ctx.set(events.append)
        try:
            ctx = await driver.pre_round()
        finally:
            _on_step_ctx.reset(token)

        assert agent._histories[sid][1:] == [
            {"role": "user", "content": "follow up 1"},
            {"role": "user", "content": "follow up 2"},
        ]
        assert [e["text"] for e in events] == ["follow up 1", "follow up 2"]
        assert all(e["type"] == "steering_injected" for e in events)
        assert ctx.quota_ok is True
    finally:
        _release_session_lock(sid, lock)


@pytest.mark.asyncio
async def test_pre_round_empty_queue_is_noop():
    sid = "s-driver-2"
    agent = _FakeAgent({sid: [{"role": "system", "content": "sys"}]})
    driver = _SteeringLongTaskDriver(sid, agent, None)

    events: list[dict] = []
    token = _on_step_ctx.set(events.append)
    try:
        await driver.pre_round()
    finally:
        _on_step_ctx.reset(token)

    assert agent._histories[sid] == [{"role": "system", "content": "sys"}]
    assert events == []


@pytest.mark.asyncio
async def test_pre_round_delegates_to_inner_driver_and_returns_its_result():
    sid = "s-driver-3"
    agent = _FakeAgent({sid: []})
    inner = _FakeInnerDriver()
    driver = _SteeringLongTaskDriver(sid, agent, inner)

    result = await driver.pre_round()
    assert inner.pre_round_called is True
    assert result == "inner-round-context"


@pytest.mark.asyncio
async def test_post_round_delegates_to_inner_when_present():
    sid = "s-driver-4"
    agent = _FakeAgent({sid: []})
    inner = _FakeInnerDriver()
    driver = _SteeringLongTaskDriver(sid, agent, inner)

    await driver.post_round({"cost_usd": 1.23})
    assert inner.post_round_called_with == {"cost_usd": 1.23}


@pytest.mark.asyncio
async def test_post_round_noop_without_inner():
    sid = "s-driver-5"
    agent = _FakeAgent({sid: []})
    driver = _SteeringLongTaskDriver(sid, agent, None)
    await driver.post_round({"cost_usd": 1.0})  # 不应抛出


@pytest.mark.asyncio
async def test_coordinator_enqueue_steering_message_end_to_end():
    coord = MasterCoordinator(max_rounds=1)
    sid = "s-coord-1"
    lock = await _acquire_session_lock(sid)
    try:
        assert coord.enqueue_steering_message(sid, "hi") is True
    finally:
        _release_session_lock(sid, lock)
        _drain_steering_messages(sid)
    assert coord.enqueue_steering_message("s-coord-idle", "hi") is False
