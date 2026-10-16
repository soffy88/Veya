"""SEC audit regression tests — H1: 全局默认拒绝 (server.app 中间件)。

审计 H1: server/app.py 只 include_router, 无全局依赖; 90 个路由文件里约 38 个
完全无鉴权。修复: 加纯 ASGI 中间件 AuthGateMiddleware —— 除显式公开白名单外,
一律要求有效 token, 否则 401。

Run: .venv/bin/python -m pytest tests/test_sec_audit_h1_default_deny.py -q
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
    monkeypatch.delenv("VEYA_OWNER_USER_ID", raising=False)
    monkeypatch.delenv("VEYA_OWNER_USER_IDS", raising=False)
    return db


@pytest.fixture
def gated_app() -> FastAPI:
    """A minimal app wearing the gate (no need to import the 449-route real app)."""
    app = FastAPI()

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok"}

    @app.get("/api/v1/secret")
    async def secret() -> dict:
        return {"secret": True}

    @app.get("/api/v1/a/{token}")
    async def shared(token: str) -> dict:
        return {"token": token}

    @app.get("/api/v1/auth/login")
    async def login() -> dict:
        return {"login": True}

    app.add_middleware(auth_mod.AuthGateMiddleware)
    return app


@pytest.fixture
def owner(auth_db: Path) -> dict:
    return auth_mod.create_user("owneruser", "a-very-long-password", is_owner=True)


# ── path classification ───────────────────────────────────────────────


def test_health_is_public() -> None:
    assert auth_mod.is_public_path("/health")
    assert auth_mod.is_public_path("/health/execution-runtime")


def test_login_and_oauth_are_public() -> None:
    for p in ("/api/v1/auth/login", "/api/v1/auth/register", "/token", "/authorize", "/register"):
        assert auth_mod.is_public_path(p), p


def test_webhooks_are_public() -> None:
    for p in (
        "/api/v1/webhooks/github",
        "/api/v1/channels/telegram/webhook",
        "/im/wechat/webhook",
        "/im/discord/webhook",
        "/automata/webhooks/x",
    ):
        assert auth_mod.is_public_path(p), p


def test_public_shares_are_public() -> None:
    assert auth_mod.is_public_path("/api/v1/a/abc123")
    assert auth_mod.is_public_path("/api/v1/h/page.html")


def test_dangerous_routes_are_not_public() -> None:
    for p in (
        "/api/v1/tools/execute",
        "/api/v1/sandbox/execute",
        "/api/v1/fs/read",
        "/api/v1/vault/secrets",
        "/auth/key",
        "/master/chat",
        "/api/v1/account/bind",
        "/api/v1/mcp/connect",
        "/api/v1/spawn/install",
    ):
        assert not auth_mod.is_public_path(p), f"{p} must NOT be public"


def test_no_blanket_api_v1_public() -> None:
    """``/api/v1/*`` 不得整体放行 (H1 的核心)。"""
    assert not auth_mod.is_public_path("/api/v1/anything/unlisted")


# ── middleware enforcement ────────────────────────────────────────────


def test_anonymous_denied_on_protected(gated_app: FastAPI) -> None:
    client = TestClient(gated_app)
    assert client.get("/api/v1/secret").status_code == 401


def test_anonymous_allowed_on_public(gated_app: FastAPI) -> None:
    client = TestClient(gated_app)
    assert client.get("/health").status_code == 200
    assert client.get("/api/v1/auth/login").status_code == 200


def test_valid_token_passes(gated_app: FastAPI, owner: dict) -> None:
    token = auth_mod.issue_token(owner["user_id"])
    client = TestClient(gated_app)
    resp = client.get("/api/v1/secret", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200


def test_query_token_passes_for_sse(gated_app: FastAPI, owner: dict) -> None:
    """EventSource 无法设 Authorization 头 → ``?token=`` 必须被接受 (SSE 场景)。"""
    token = auth_mod.issue_token(owner["user_id"])
    client = TestClient(gated_app)
    assert client.get(f"/api/v1/secret?token={token}").status_code == 200


def test_cookie_token_passes(gated_app: FastAPI, owner: dict) -> None:
    token = auth_mod.issue_token(owner["user_id"])
    client = TestClient(gated_app)
    resp = client.get("/api/v1/secret", cookies={"veya_token": token})
    assert resp.status_code == 200


def test_bad_token_denied(gated_app: FastAPI) -> None:
    client = TestClient(gated_app)
    assert client.get("/api/v1/secret", headers={"Authorization": "Bearer nope"}).status_code == 401


def test_digest_not_accepted_as_credential(gated_app: FastAPI, owner: dict) -> None:
    """H2 联动: 数据库里的 sha256 摘要不能当 bearer 凭据用。"""
    token = auth_mod.issue_token(owner["user_id"])
    digest = auth_mod.hash_token(token)
    client = TestClient(gated_app)
    assert (
        client.get("/api/v1/secret", headers={"Authorization": f"Bearer {digest}"}).status_code
        == 401
    )


def test_local_single_user_loopback(gated_app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VEYA_LOCAL_SINGLE_USER", "1")
    client = TestClient(gated_app, client=("127.0.0.1", 5000))
    assert client.get("/api/v1/secret").status_code == 200


def test_local_single_user_off_by_default(gated_app: FastAPI) -> None:
    client = TestClient(gated_app, client=("127.0.0.1", 5000))
    assert client.get("/api/v1/secret").status_code == 401
