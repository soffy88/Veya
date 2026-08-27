"""server/routes/session.py + sessions.py — 多账号隔离回归 (2026-08-16)。

此前这两个路由完全没有鉴权/归属校验: 任何人凭 session_id 就能读别人的完整
会话内容, `/sessions` 能列出全站所有账号的会话。直接调用路由函数 (FastAPI
的 async 函数, Depends 参数手动传入即可), 不起完整 HTTP server, 更快更聚焦。
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from server.routes import session as session_routes
from server.routes.session import (
    SessionCreateRequest,
    SessionForkRequest,
    create_session,
    fork_session,
    get_session,
    session_lineage,
    share_session,
    undo_session,
)
from server.routes.sessions import list_sessions

_ALICE = {"user_id": "alice", "username": "alice"}
_BOB = {"user_id": "bob", "username": "bob"}


@pytest.fixture(autouse=True)
def _clean_session_state():
    """路由模块的 _sessions/_undo_stacks 是进程级全局字典, 测试间需要隔离。"""
    session_routes._sessions.clear()
    session_routes._undo_stacks.clear()
    session_routes._shares.clear()
    yield
    session_routes._sessions.clear()
    session_routes._undo_stacks.clear()
    session_routes._shares.clear()


async def _make_session(user: dict) -> str:
    resp = await create_session(SessionCreateRequest(), user=user)
    return resp["id"]


# ── create_session: 写入归属 ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_session_records_owner():
    sid = await _make_session(_ALICE)
    assert session_routes._sessions[sid]["user_id"] == "alice"


# ── get_session: 跨账号读取被拒绝 ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_session_owner_can_read_own_session():
    sid = await _make_session(_ALICE)
    s = await get_session(sid, user=_ALICE)
    assert s["id"] == sid


@pytest.mark.asyncio
async def test_get_session_other_user_gets_404_not_leaked_content():
    """回归: 此前任何人凭 session_id 就能读到完整会话内容, 不需要登录。"""
    sid = await _make_session(_ALICE)
    with pytest.raises(HTTPException) as exc:
        await get_session(sid, user=_BOB)
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_get_session_unauthenticated_anonymous_cannot_read_others():
    sid = await _make_session(_ALICE)
    anon = {"user_id": "anonymous", "username": "anonymous"}
    with pytest.raises(HTTPException):
        await get_session(sid, user=anon)


# ── list_sessions: 只看得到自己的 ────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_sessions_only_shows_own_sessions():
    """回归: 此前 GET /sessions 列出全站所有用户的会话, 不做任何过滤。"""
    await _make_session(_ALICE)
    await _make_session(_ALICE)
    await _make_session(_BOB)

    alice_view = await list_sessions(user=_ALICE)
    bob_view = await list_sessions(user=_BOB)

    assert alice_view["total"] == 2
    assert bob_view["total"] == 1


# ── fork / compact / undo / share: 非 owner 一律 404 ─────────────────────


@pytest.mark.asyncio
async def test_fork_session_rejects_non_owner():
    sid = await _make_session(_ALICE)
    with pytest.raises(HTTPException):
        await fork_session(sid, SessionForkRequest(), user=_BOB)


@pytest.mark.asyncio
async def test_fork_session_owner_succeeds_and_child_inherits_owner():
    sid = await _make_session(_ALICE)
    result = await fork_session(sid, SessionForkRequest(label="x"), user=_ALICE)
    new_id = result["session_id"]
    assert session_routes._sessions[new_id]["user_id"] == "alice"


@pytest.mark.asyncio
async def test_undo_session_rejects_non_owner():
    sid = await _make_session(_ALICE)
    with pytest.raises(HTTPException):
        await undo_session(sid, user=_BOB)


@pytest.mark.asyncio
async def test_share_session_rejects_non_owner():
    sid = await _make_session(_ALICE)
    with pytest.raises(HTTPException):
        await share_session(sid, user=_BOB)


# ── lineage: ancestors/descendants 按归属过滤 ────────────────────────────


@pytest.mark.asyncio
async def test_session_lineage_rejects_non_owner():
    sid = await _make_session(_ALICE)
    with pytest.raises(HTTPException):
        await session_lineage(sid, user=_BOB)


@pytest.mark.asyncio
async def test_session_lineage_descendants_filtered_by_owner():
    sid = await _make_session(_ALICE)
    # 直接写入 _sessions (而非走 fork_session —— 它目前不写 forked_from 字段,
    # 是与本次安全修复无关的既有 bug, 不在这次范围内顺手修), 分别构造一条
    # Alice 名下、一条 Bob 名下、都 forked_from 同一个 sid 的会话。
    session_routes._sessions["alice_child"] = {
        "id": "alice_child",
        "user_id": "alice",
        "forked_from": sid,
        "messages": [],
    }
    session_routes._sessions["bob_forged"] = {
        "id": "bob_forged",
        "user_id": "bob",
        "forked_from": sid,
        "messages": [],
    }

    lineage = await session_lineage(sid, user=_ALICE)
    descendant_ids = {d["id"] for d in lineage["descendants"]}
    assert descendant_ids == {"alice_child"}
    assert "bob_forged" not in descendant_ids
