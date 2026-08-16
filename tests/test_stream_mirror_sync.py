"""跨端同步: 会话流事件镜像到每用户扇出通道 (电脑执行时手机实时跟随)。

notification_center.push_stream 是逐帧高频镜像 — 只推给同 user_id 的连接,
不注册进 _messages (逐 token 会内存泄漏), 用独立 type="STREAM" 帧。
"""

from __future__ import annotations

import asyncio

import pytest

from server.notification_center import NotificationCenter


@pytest.mark.asyncio
async def test_push_stream_fans_out_to_same_user_only():
    nc = NotificationCenter()
    q_me = nc.connect(user_id="alice")
    q_other = nc.connect(user_id="bob")
    q_anon = nc.connect(user_id="")

    nc.push_stream("sid-1", {"type": "text_delta", "delta": "你好"}, user_id="alice")

    frame = q_me.get_nowait()
    assert frame["type"] == "STREAM"
    assert frame["session_id"] == "sid-1"
    assert frame["event"] == {"type": "text_delta", "delta": "你好"}
    # 其它用户 / 匿名 收不到 (跨用户隔离)
    assert q_other.empty()
    assert q_anon.empty()


@pytest.mark.asyncio
async def test_push_stream_anonymous_noop():
    nc = NotificationCenter()
    q = nc.connect(user_id="alice")
    # user_id 空 → 不镜像 (无法按用户隔离, 跨端无意义)
    nc.push_stream("sid-1", {"type": "text_delta", "delta": "x"}, user_id="")
    assert q.empty()


@pytest.mark.asyncio
async def test_push_stream_does_not_leak_messages_registry():
    nc = NotificationCenter()
    nc.connect(user_id="alice")
    # 逐 token 高频镜像绝不能进 _messages (否则无限增长)
    for i in range(100):
        nc.push_stream("sid", {"type": "text_delta", "delta": str(i)}, user_id="alice")
    assert len(nc._messages) == 0


@pytest.mark.asyncio
async def test_stream_pump_mirrors_events_for_logged_in_user(monkeypatch):
    """new_agent_stream_events 消费队列时, 每帧镜像到 push_stream。"""
    import server.chat_stream as cs
    import server.notification_center as ncmod

    captured: list[tuple[str, dict]] = []
    # chat_stream 内部 `from server.notification_center import global_notifier` —
    # 取到的是源模块的单例对象, 直接 patch 该对象的方法即可。
    monkeypatch.setattr(
        ncmod.global_notifier,
        "push_stream",
        lambda session_id, event, user_id="": captured.append((session_id, event)),
    )

    # master_coordinator.chat_stream 打桩: 往队列灌两帧后收尾。
    # 必须 patch 类方法 (非实例属性) — 实例 setattr 在 teardown 会用 bound
    # method 永久遮蔽类方法, 污染后续测试。
    from server.coordinator_master import MasterCoordinator
    from server.sse import get_or_create_queue

    async def fake_chat_stream(self, text, *, session_id=None, **kw):
        q = get_or_create_queue(session_id)
        q.on_step({"type": "text_delta", "squad_id": "master", "delta": "片段A"})
        q.on_step({"type": "text_delta", "squad_id": "master", "delta": "片段B"})
        return {"status": "success", "final_answer": "片段A片段B"}

    monkeypatch.setattr(MasterCoordinator, "chat_stream", fake_chat_stream)

    async for _frame in cs.new_agent_stream_events(
        "你好", session_id="sid-sync", user={"user_id": "alice", "username": "alice"}
    ):
        pass

    # 镜像里应包含 user_prompt (首帧) + 流式 text_delta (2 帧) + _finish 补发的
    # 最终答案 text_delta + master_done。
    kinds = [ev.get("type") for _sid, ev in captured]
    assert "user_prompt" in kinds
    assert kinds.count("text_delta") >= 2
    assert "master_done" in kinds
    assert all(sid == "sid-sync" for sid, _ in captured)


@pytest.mark.asyncio
async def test_stream_pump_no_mirror_for_anonymous(monkeypatch):
    """未登录 (user=None) → 不镜像 (push_stream 不被调用)。"""
    import server.chat_stream as cs
    import server.notification_center as ncmod

    calls: list = []
    monkeypatch.setattr(
        ncmod.global_notifier,
        "push_stream",
        lambda *a, **k: calls.append((a, k)),
    )

    from server.coordinator_master import MasterCoordinator
    from server.sse import get_or_create_queue

    async def fake_chat_stream(self, text, *, session_id=None, **kw):
        q = get_or_create_queue(session_id)
        q.on_step({"type": "text_delta", "squad_id": "master", "delta": "x"})
        return {"status": "success", "final_answer": "x"}

    monkeypatch.setattr(MasterCoordinator, "chat_stream", fake_chat_stream)

    gen = cs.new_agent_stream_events("你好", session_id="sid-anon", user=None)
    async for _frame in gen:
        pass

    assert calls == []
