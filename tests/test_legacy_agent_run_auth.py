"""POST /api/v1/agent/run 鉴权回归 (2026-08-16)。

此前该路由 (server/routes/legacy_agent.py::legacy_agent_run) 没有
Depends(auth_mod.get_current_user)——即便带了 Authorization: Bearer token，
也完全不会被解析，请求一律落进共享的 anonymous 历史/记忆桶。同文件的
/stream 端点一直是有这个依赖的，两者行为不一致。
"""

from __future__ import annotations

import inspect

import pytest

from server import auth as auth_mod
from server.routes.legacy_agent import LegacyAgentRunRequest, legacy_agent_run


def test_legacy_agent_run_declares_auth_dependency():
    """签名里必须有走 auth_mod.get_current_user 的 Depends 参数。"""
    sig = inspect.signature(legacy_agent_run)
    user_param = sig.parameters.get("user")
    assert user_param is not None, "legacy_agent_run 缺少 user 参数"
    default = user_param.default
    assert getattr(default, "dependency", None) is auth_mod.get_current_user


@pytest.mark.asyncio
async def test_legacy_agent_run_uses_the_authenticated_user_not_anonymous(monkeypatch, tmp_path):
    """核心断言: 走真正的 get_current_user 依赖解析出一个已登录用户后,

    调用期间 auth.current_user() 必须反映这个用户——而不是硬编码/遗漏解析
    导致的 anonymous (此前 bug 的表现: 哪怕调用方带了合法 token, 请求也会
    被当匿名处理, 历史/记忆全部串进共享桶)。直接传 user= 参数不会触发
    get_current_user 内部的 contextvar 设置, 所以这里显式先走一遍它，
    模拟 FastAPI 依赖注入实际发生的事情。用临时数据库, 不脏写真实
    ~/.veya/auth.db。
    """
    # 重定向到临时库 + 重新建表 (_DB_PATH 是模块级路径, create_user 等都读它)
    monkeypatch.setattr(auth_mod, "_DB_PATH", tmp_path / "auth.db")
    auth_mod._init_db()

    seen_uid: list[str] = []

    async def _fake_chat_stream(self, text, **kwargs):
        seen_uid.append(auth_mod.current_user()["user_id"])
        return {"session_id": "sid1", "status": "success", "final_answer": "ok", "cost_usd": 0.0}

    import server.coordinator_master as cm

    monkeypatch.setattr(cm.MasterCoordinator, "chat_stream", _fake_chat_stream)

    created = auth_mod.create_user("legacy_run_test_user", "testpass123")
    token = auth_mod.issue_token(created["user_id"])
    user = auth_mod.get_current_user(authorization=f"Bearer {token}")

    req = LegacyAgentRunRequest(text="hi")
    await legacy_agent_run(req, user=user)

    assert seen_uid == [created["user_id"]]
    assert created["user_id"] != "anonymous"
