"""P1-1: 管理类 / 审批类 / 密钥类 / 执行类路由必须要求 owner (SEC H1/Owner 模型).

验证:
1. 匿名调用 (无 token) -> 401 Unauthorized
2. 普通登录用户 (非 owner) -> 403 Forbidden
3. Owner 用户 -> 鉴权通过 (非 401 / 非 403, 正常进入业务逻辑)
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from server import auth as auth_mod
from server.app import app


@pytest.fixture
def auth_setup(tmp_path, monkeypatch):
    """设置隔离的 auth.db, 创建 owner 和 member 用户及对应 token."""
    db = tmp_path / "auth_test.db"
    monkeypatch.setattr(auth_mod, "_DB_PATH", db)
    monkeypatch.delenv("VEYA_LOCAL_SINGLE_USER", raising=False)
    monkeypatch.delenv("VEYA_OWNER_USER_ID", raising=False)
    monkeypatch.delenv("VEYA_OWNER_USER_IDS", raising=False)
    auth_mod._user_ctx.set(None)

    auth_mod._DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with auth_mod._lock, auth_mod._connect() as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS users ("
            "  id TEXT PRIMARY KEY, username TEXT NOT NULL UNIQUE,"
            "  password_hash TEXT NOT NULL, created_at INTEGER NOT NULL,"
            "  is_owner INTEGER NOT NULL DEFAULT 0)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS tokens ("
            "  token TEXT PRIMARY KEY, user_id TEXT NOT NULL,"
            "  created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL)"
        )

    owner = auth_mod.create_user("owner_user", "password_123456", is_owner=True)
    member = auth_mod.create_user("member_user", "password_123456", is_owner=False)

    owner_tok = auth_mod.issue_token(owner["user_id"])
    member_tok = auth_mod.issue_token(member["user_id"])

    return {
        "owner": {"headers": {"Authorization": f"Bearer {owner_tok}"}, "user": owner},
        "member": {"headers": {"Authorization": f"Bearer {member_tok}"}, "user": member},
    }


MANAGED_ENDPOINTS = [
    # method, path, json_body
    ("POST", "/vault/tasks/task_123/approve", {"approved": True}),
    ("POST", "/api/v1/tasks/task_123/approve", {"approved": True}),
    ("GET", "/vault/secrets", None),
    ("POST", "/vault/secrets", {"vault_id": "v1", "secret": "s1"}),
    ("GET", "/vault/pending", None),
    ("POST", "/permission/req_123/approve", {"note": "test approve"}),
    ("POST", "/permission/req_123/deny", {"note": "test deny"}),
    ("GET", "/permission/pending", None),
    ("POST", "/notifications/notif_123/approve", {"approved": True}),
    ("GET", "/tool", None),
    ("POST", "/tool", {"name": "read", "args": {"path": "/tmp/test"}}),
    ("GET", "/security/permissions", None),
    ("GET", "/security/audit-logs", None),
    ("GET", "/automata/jobs", None),
    ("POST", "/automata/jobs", {"cron_expr": "* * * * *", "task_prompt": "echo hi"}),
    ("DELETE", "/automata/jobs/job_123", None),
    ("POST", "/automata/run-now", {"task_prompt": "echo hi"}),
    ("POST", "/autonomous/execute", {"plan_id": "plan_123"}),
]


@pytest.mark.parametrize("method,path,body", MANAGED_ENDPOINTS)
def test_managed_endpoint_requires_owner(auth_setup, method, path, body):
    from unittest.mock import AsyncMock, MagicMock

    from server.routes import automata as automata_routes

    # 避免测试环境 TestClient 同步运行导致 apscheduler 旧 loop 关闭报错
    mock_auto = MagicMock()
    mock_auto.register_cron_task.return_value = "ok"
    mock_auto.remove_task.return_value = "ok"
    mock_auto.get_jobs.return_value = []
    mock_auto.get_recent_results.return_value = []
    mock_auto._run_headless_mission = AsyncMock(return_value=None)
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(automata_routes, "get_automata", lambda: mock_auto)

    client = TestClient(app)

    # 1. 匿名访问 -> 401
    resp_anon = client.request(method, path, json=body)
    assert resp_anon.status_code == 401, f"{method} {path} anon expected 401, got {resp_anon.status_code}: {resp_anon.text}"

    # 2. 普通登录用户 (非 owner) -> 403
    resp_member = client.request(method, path, json=body, headers=auth_setup["member"]["headers"])
    assert resp_member.status_code == 403, f"{method} {path} member expected 403, got {resp_member.status_code}: {resp_member.text}"

    # 3. Owner 用户 -> 通过权限门禁 (状态码不能是 401 或 403)
    resp_owner = client.request(method, path, json=body, headers=auth_setup["owner"]["headers"])
    assert resp_owner.status_code not in (401, 403), f"{method} {path} owner failed auth with {resp_owner.status_code}: {resp_owner.text}"
