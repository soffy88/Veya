"""GET /api/v1/agent/sessions + /api/v1/agent/history/{sid} — 多端同步

数据源切换回归 (2026-08-16)。

此前这两个"多端同步"接口读的是 veya.history_store (SqliteHistoryStore)，
但生产实际路径 (VEYA_AGENT_LOOP=strict) 的对话只写进 session_tree.db
(SessionTreeMgr)，两者从未打通——同一账号换设备登录，看不到任何新对话，
因为查询的存储压根没有数据。
"""

from __future__ import annotations

import pytest

from server.routes.legacy_agent import get_session_history, list_user_sessions
from veya.obase.adapters import SqliteKvStore
from veya.omodul.session_tree import SessionTreeMgr

_ALICE = {"user_id": "alice", "username": "alice"}
_BOB = {"user_id": "bob", "username": "bob"}


def _tree(tmp_path) -> SessionTreeMgr:
    return SessionTreeMgr(kv=SqliteKvStore(str(tmp_path / "session.db")))


@pytest.fixture(autouse=True)
def _patch_session_kv(tmp_path, monkeypatch):
    """两个路由内部各自 import server.agent_loop_bridge._session_kv() 取默认

    存储；重定向到临时文件，同时把 helper 拿出来给测试直接建同一份数据。
    """
    import server.agent_loop_bridge as bridge_mod

    kv_path = str(tmp_path / "session.db")
    monkeypatch.setattr(bridge_mod, "_session_kv", lambda *a, **kw: SqliteKvStore(kv_path))
    return kv_path


@pytest.mark.asyncio
async def test_list_user_sessions_only_shows_own_sessions(_patch_session_kv):
    tree = SessionTreeMgr(kv=SqliteKvStore(_patch_session_kv))
    sid_a = tree.create_session(system="sys", owner="alice")
    tree.append(sid_a, role="user", content="alice 的会话")
    sid_b = tree.create_session(system="sys", owner="bob")
    tree.append(sid_b, role="user", content="bob 的会话")

    result = await list_user_sessions(user=_ALICE)
    assert [s["sid"] for s in result["sessions"]] == [sid_a]
    assert result["user_id"] == "alice"


@pytest.mark.asyncio
async def test_get_session_history_returns_own_messages_without_system_prompt(
    _patch_session_kv,
):
    tree = SessionTreeMgr(kv=SqliteKvStore(_patch_session_kv))
    sid = tree.create_session(system="sys prompt", owner="alice")
    tree.append(sid, role="user", content="你好")
    tree.append(sid, role="assistant", content="你好，有什么可以帮你")

    result = await get_session_history(sid, user=_ALICE)
    roles = [m["role"] for m in result["messages"]]
    assert roles == ["user", "assistant"]  # system 已被剔除


@pytest.mark.asyncio
async def test_get_session_history_rejects_other_users_session(_patch_session_kv):
    """回归核心断言: 拿到别人的 sid, 读不到内容 (不是空列表以外的任何数据)。"""
    tree = SessionTreeMgr(kv=SqliteKvStore(_patch_session_kv))
    sid = tree.create_session(system="sys", owner="alice")
    tree.append(sid, role="user", content="alice 的隐私内容")

    result = await get_session_history(sid, user=_BOB)
    assert result["messages"] == []


@pytest.mark.asyncio
async def test_get_session_history_legacy_ownerless_session_is_readable(_patch_session_kv):
    """旧数据 (早于归属校验修复, owner=None) 不因这次修复变得完全不可读——

    向后兼容, 与仓库其它归属校验点口径一致。
    """
    tree = SessionTreeMgr(kv=SqliteKvStore(_patch_session_kv))
    sid = tree.create_session(system="sys")  # 无 owner
    tree.append(sid, role="user", content="旧数据")

    result = await get_session_history(sid, user=_BOB)
    assert len(result["messages"]) == 1
