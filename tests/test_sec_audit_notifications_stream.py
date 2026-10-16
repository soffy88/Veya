"""SEC audit regression tests — item 15: /notifications/stream 必须鉴权。

审计: /legacy/notifications/stream (经 SvelteKit 转发到后端 /notifications/stream)
公网未鉴权 200, 会泄露通知数据。修复: require_user + 全局默认拒绝中间件; 前端
带 Bearer / ?token= 保持可用。

Run: .venv/bin/python -m pytest tests/test_sec_audit_notifications_stream.py -q
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
    from server.routes.notifications import router

    app = FastAPI()
    app.include_router(router)
    app.add_middleware(auth_mod.AuthGateMiddleware)
    return app


@pytest.fixture
def member_token(auth_db: Path) -> str:
    user = auth_mod.create_user("memberuser", "a-very-long-password")
    return auth_mod.issue_token(user["user_id"])


def test_notifications_stream_anonymous_denied(app: FastAPI) -> None:
    client = TestClient(app)
    assert client.get("/notifications/stream").status_code == 401


@pytest.fixture
def finite_stream(monkeypatch: pytest.MonkeyPatch):
    """Replace the infinite SSE source with one finite frame so the request ends."""
    from server import notification_center

    async def _finite(user_id: str = ""):
        yield f'data: {{"user_id": "{user_id}"}}\n\n'

    monkeypatch.setattr(notification_center.global_notifier, "stream", _finite)


def test_notifications_stream_query_token_allowed(
    app: FastAPI, member_token: str, finite_stream
) -> None:
    """EventSource 无法设头 → ?token= 必须可用 (前端保持可用)。"""
    client = TestClient(app)
    resp = client.get(f"/notifications/stream?token={member_token}")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")


def test_notifications_stream_bearer_allowed(
    app: FastAPI, member_token: str, finite_stream
) -> None:
    client = TestClient(app)
    resp = client.get("/notifications/stream", headers={"Authorization": f"Bearer {member_token}"})
    assert resp.status_code == 200


def test_approve_endpoint_requires_auth(app: FastAPI) -> None:
    client = TestClient(app)
    assert client.post("/notifications/x/approve", json={"approved": True}).status_code == 401
