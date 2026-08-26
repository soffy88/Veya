"""GET /api/v1/agent/sessions + /api/v1/agent/history/{sid} — 多端同步

数据源切换回归 (2026-08-17)。

架构澄清后 (docs/ARCHITECTURE_STABLE.md「冻结架构」), MasterAgent ReAct 恢复
为唯一主链, 用户对话权威写入 veya.history_store (SqliteHistoryStore, 见
coordinator_master._persist_history) —— 这两个"多端同步"接口相应改回读它
(此前短暂改读 omodul 的 session_tree.db, 因为那段时间生产实际路径是
VEYA_AGENT_LOOP=strict; 现在 strict 只服务 agent_loop_run 隔离子任务, 不再
写用户会话历史, session_tree.db 对多端同步已不相关)。history_store 按
(user_id, sid) 联合主键分区, 归属隔离是存储层结构性保证, 不需要额外校验。
"""

from __future__ import annotations

import pytest

from server.routes.legacy_agent import attach_session, get_session_history, list_user_sessions
from veya.history_store import SqliteHistoryStore

_ALICE = {"user_id": "alice", "username": "alice"}
_BOB = {"user_id": "bob", "username": "bob"}


@pytest.fixture(autouse=True)
def _patch_history_store(tmp_path, monkeypatch):
    """两个路由内部各自 import veya.history_store.default_history_store() 取

    进程级单例；重定向到临时文件级实例，避免测试间/跨会话串数据。
    """
    import veya.history_store as history_store_mod

    store = SqliteHistoryStore(tmp_path / "history.db")
    monkeypatch.setattr(history_store_mod, "default_history_store", lambda: store)
    return store


@pytest.mark.asyncio
async def test_list_user_sessions_only_shows_own_sessions(_patch_history_store):
    store = _patch_history_store
    await store.save("sid-a", [{"role": "user", "content": "alice 的会话"}], user_id="alice")
    await store.save("sid-b", [{"role": "user", "content": "bob 的会话"}], user_id="bob")

    result = await list_user_sessions(user=_ALICE)
    assert [s["sid"] for s in result["sessions"]] == ["sid-a"]
    assert result["user_id"] == "alice"


@pytest.mark.asyncio
async def test_get_session_history_returns_own_messages(_patch_history_store):
    store = _patch_history_store
    await store.save(
        "sid-a",
        [
            {"role": "user", "content": "你好"},
            {"role": "assistant", "content": "你好，有什么可以帮你"},
        ],
        user_id="alice",
    )

    result = await get_session_history("sid-a", user=_ALICE)
    roles = [m["role"] for m in result["messages"]]
    assert roles == ["user", "assistant"]


@pytest.mark.asyncio
async def test_get_session_history_rejects_other_users_session(_patch_history_store):
    """回归核心断言: 拿到别人的 sid, 读不到内容 (存储按 user_id 分区, 天然隔离)。"""
    store = _patch_history_store
    await store.save("sid-a", [{"role": "user", "content": "alice 的隐私内容"}], user_id="alice")

    result = await get_session_history("sid-a", user=_BOB)
    assert result["messages"] == []


@pytest.mark.asyncio
async def test_attach_session_returns_history_and_inactive_by_default(_patch_history_store):
    """docs/VEYA_P1_P3_IMPLEMENTATION_SPEC.md §5 attach: 没有 _active_streams 登记时

    active=false (跟 /api/v1/agent/stream_status 同一份判断逻辑)。
    """
    store = _patch_history_store
    await store.save("sid-a", [{"role": "user", "content": "你好"}], user_id="alice")

    result = await attach_session("sid-a", user=_ALICE)
    assert result["session_id"] == "sid-a"
    assert [m["role"] for m in result["messages"]] == ["user"]
    assert result["active"] is False


@pytest.mark.asyncio
async def test_attach_session_reflects_active_running_turn(_patch_history_store, monkeypatch):
    import asyncio

    from server import coordinator_master

    async def _never_ends():
        await asyncio.sleep(10)

    task = asyncio.create_task(_never_ends())
    monkeypatch.setitem(coordinator_master._active_streams, "sid-a", task)
    try:
        result = await attach_session("sid-a", user=_ALICE)
        assert result["active"] is True
    finally:
        task.cancel()


@pytest.mark.asyncio
async def test_attach_session_rejects_other_users_session(_patch_history_store):
    store = _patch_history_store
    await store.save("sid-a", [{"role": "user", "content": "alice 的隐私内容"}], user_id="alice")

    result = await attach_session("sid-a", user=_BOB)
    assert result["messages"] == []
