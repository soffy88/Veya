"""SEC audit regression tests — C4: master/chat 请求级 endpoint 不得泄露服务端 key。

审计 C4: ``POST /master/chat`` 无鉴权, 且可指定任意 ``endpoint``; ``get_api_key``
在请求未带 key 时回落到服务端环境变量里的 key, 于是服务端 key 被发往攻击者端点。

修复: (1) 路由 require_user; (2) 请求级 endpoint 只在调用方同时提供自有
``user_api_key`` 时才生效 (路由与 coordinator 双重把关)。

Run: .venv/bin/python -m pytest tests/test_sec_audit_c4_master_endpoint.py -q
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server import auth as auth_mod


@pytest.fixture
def auth_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db = tmp_path / "auth.db"
    monkeypatch.setattr(auth_mod, "_DB_PATH", db)
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
    auth_mod._user_ctx.set(None)
    monkeypatch.delenv("VEYA_LOCAL_SINGLE_USER", raising=False)
    return db


@pytest.fixture
def app() -> FastAPI:
    from server.routes.master import router

    app = FastAPI()
    app.include_router(router)
    app.add_middleware(auth_mod.AuthGateMiddleware)
    return app


@pytest.fixture
def member_token(auth_db: Path) -> str:
    user = auth_mod.create_user("memberuser", "a-very-long-password", is_owner=False)
    return auth_mod.issue_token(user["user_id"])


def test_master_chat_requires_auth(app: FastAPI) -> None:
    client = TestClient(app)
    assert client.post("/master/chat", json={"text": "hi"}).status_code == 401


def test_endpoint_ignored_without_user_key(app: FastAPI, member_token: str) -> None:
    """无自有 key 时, 攻击者指定的 endpoint 必须被忽略 (服务端 key 不外泄)。"""
    from server.coordinator_master import MasterCoordinator

    captured: dict = {}

    class _Fake(MasterCoordinator):
        def __init__(self, **kw):  # noqa: D401
            captured.update(kw)

        async def chat_stream(self, *a, **k):
            return {"ok": True}

    import server.routes.master as m

    orig = m.MasterCoordinator
    m.MasterCoordinator = _Fake
    try:
        client = TestClient(app)
        resp = client.post(
            "/master/chat",
            json={"text": "hi", "endpoint": "https://attacker.example/v1"},
            headers={"Authorization": f"Bearer {member_token}"},
        )
    finally:
        m.MasterCoordinator = orig

    assert resp.status_code == 200
    assert captured.get("endpoint") is None, "endpoint must be dropped without a user key"


def test_coordinator_drops_endpoint_without_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """纵深防御: coordinator 自身也不接受无 key 的请求级 endpoint。"""
    from server.coordinator_master import MasterCoordinator

    seen: dict = {}

    def _fake_get_provider_config(*a, **k):
        return ("openai", {})

    monkeypatch.setattr("server.coordinator_master.get_provider_config", _fake_get_provider_config)

    coord = MasterCoordinator(endpoint="https://attacker.example/v1")
    seen["endpoint"] = coord.endpoint
    assert seen["endpoint"] is None

    coord2 = MasterCoordinator(endpoint="https://attacker.example/v1", user_api_key="user-key")
    assert coord2.endpoint == "https://attacker.example/v1"
