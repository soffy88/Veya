"""server/checkpoint.py + resume_session — 归属校验回归 (2026-08-16)。

此前 checkpoint 落盘完全没有 user_id 概念，/session/{id}/resume 只拦"必须
登录"，拦不到"这个 checkpoint 是不是你的"。用 monkeypatch 把落盘目录重定向
到临时目录，不脏写真实 ~/.veya/checkpoints。
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from server import auth as auth_mod
from server import checkpoint as checkpoint_mod
from server.routes.session import resume_session
from veya.obase.compat import RunState

_ALICE = {"user_id": "alice", "username": "alice"}
_BOB = {"user_id": "bob", "username": "bob"}


@pytest.fixture(autouse=True)
def _isolated_checkpoint_dir(tmp_path, monkeypatch):
    # server/checkpoint.py 和 veya.obase.compat.restore_from_checkpoint 是两套
    # 各自持有 _CHECKPOINT_DIR 拷贝的独立实现 (resume_session 两个都用), 必须
    # 一起重定向, 否则 save 落到 tmp_path 但 restore 还读真实 ~/.veya。
    import veya.obase.compat as compat_mod

    monkeypatch.setattr(checkpoint_mod, "_CKPT_DIR", tmp_path)
    monkeypatch.setattr(compat_mod, "_CHECKPOINT_DIR", tmp_path)


async def _save_as(user: dict, sid: str) -> None:
    token = auth_mod._user_ctx.set(user)
    try:
        await checkpoint_mod.save_checkpoint(sid, RunState(session_id=sid))
    finally:
        auth_mod._user_ctx.reset(token)


@pytest.mark.asyncio
async def test_save_checkpoint_records_current_user_as_owner():
    await _save_as(_ALICE, "sid1")
    ckpt = await checkpoint_mod.load_checkpoint("sid1")
    assert ckpt.owner == "alice"


@pytest.mark.asyncio
async def test_load_checkpoint_missing_owner_is_none_for_legacy_data(tmp_path):
    """回归: 早于本次修复写入的旧数据没有 owner 字段, 读出来应是 None (无主),

    不能被误判成属于某个具体账号。
    """
    import json

    legacy_path = tmp_path / "legacy-sid.jsonl"
    legacy_path.write_text(
        json.dumps({"session_id": "legacy-sid", "timestamp": 1.0, "version": 1, "payload": {}})
        + "\n"
    )
    ckpt = await checkpoint_mod.load_checkpoint("legacy-sid")
    assert ckpt.owner is None


@pytest.mark.asyncio
async def test_resume_session_rejects_other_users_checkpoint():
    """回归核心断言: 拿到别人的 session_id 调 /resume, 必须被拒绝, 而不是

    静默续跑别人的任务状态。
    """
    await _save_as(_ALICE, "sid2")
    with pytest.raises(HTTPException) as exc:
        await resume_session("sid2", user=_BOB)
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_resume_session_owner_can_resume_own_checkpoint(monkeypatch):
    await _save_as(_ALICE, "sid3")

    async def _fake_resume(self, run_state):
        return {"status": "resumed", "session_id": run_state.session_id}

    import server.coordinator as coordinator_mod

    monkeypatch.setattr(coordinator_mod.Coordinator, "resume", _fake_resume)

    result = await resume_session("sid3", user=_ALICE)
    assert result["status"] == "resumed"


@pytest.mark.asyncio
async def test_resume_session_legacy_checkpoint_without_owner_is_not_blocked(monkeypatch):
    """旧数据 (无 owner 记录) 不因这次修复变得不可恢复——向后兼容。"""
    import json

    from server.checkpoint import _CKPT_DIR

    legacy_path = _CKPT_DIR / "legacy-sid2.jsonl"
    legacy_path.write_text(
        json.dumps({"session_id": "legacy-sid2", "timestamp": 1.0, "version": 1, "payload": {}})
        + "\n"
    )

    async def _fake_resume(self, run_state):
        return {"status": "resumed"}

    import server.coordinator as coordinator_mod

    monkeypatch.setattr(coordinator_mod.Coordinator, "resume", _fake_resume)

    result = await resume_session("legacy-sid2", user=_BOB)
    assert result["status"] == "resumed"
