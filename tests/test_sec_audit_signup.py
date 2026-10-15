"""SEC audit regression tests — 注册默认关闭 + /auth/key owner 门禁。

审计: ``/api/v1/auth/register`` 无开关, 注册即发 token → 攻击者拿到
``require_user`` 通行证 (配合 H2/C3 可接管)。``/auth/key`` 未鉴权即可替换全局
provider key (劫持全站 LLM 流量)。

Run: .venv/bin/python -m pytest tests/test_sec_audit_signup.py -q
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server import auth as auth_mod
from server.routes.auth import key_router, router as auth_router


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
    monkeypatch.delenv("VEYA_OWNER_USER_ID", raising=False)
    monkeypatch.delenv("VEYA_OWNER_USER_IDS", raising=False)
    monkeypatch.delenv("VEYA_PUBLIC_SIGNUP", raising=False)
    return db


@pytest.fixture
def app() -> FastAPI:
    app = FastAPI()
    app.include_router(auth_router)
    app.include_router(key_router)
    app.add_middleware(auth_mod.AuthGateMiddleware)
    return app


def test_public_signup_off_by_default() -> None:
    assert auth_mod.public_signup_enabled() is False


def test_register_closed_by_default(app: FastAPI) -> None:
    client = TestClient(app)
    resp = client.post(
        "/api/v1/auth/register", json={"username": "newbie", "password": "longenough123"}
    )
    assert resp.status_code == 403


def test_register_open_when_enabled(app: FastAPI, auth_db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VEYA_PUBLIC_SIGNUP", "1")
    client = TestClient(app)
    resp = client.post(
        "/api/v1/auth/register", json={"username": "newbie_open", "password": "longenough123"}
    )
    assert resp.status_code == 200
    assert resp.json()["token"]


def test_login_still_public(app: FastAPI, auth_db: Path) -> None:
    auth_mod.create_user("existing", "a-very-long-password")
    client = TestClient(app)
    resp = client.post(
        "/api/v1/auth/login", json={"username": "existing", "password": "a-very-long-password"}
    )
    assert resp.status_code == 200


def test_auth_key_requires_owner(app: FastAPI, auth_db: Path) -> None:
    """匿名 / 非 owner 不得替换全局 provider key。"""
    client = TestClient(app)
    assert client.post("/auth/key", json={"provider": "openai", "api_key": "x"}).status_code == 401

    member = auth_mod.create_user("member_key", "a-very-long-password", is_owner=False)
    token = auth_mod.issue_token(member["user_id"])
    resp = client.post(
        "/auth/key",
        json={"provider": "openai", "api_key": "x"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 403


def test_me_requires_auth(app: FastAPI) -> None:
    client = TestClient(app)
    assert client.get("/api/v1/auth/me").status_code == 401
