"""SEC audit regression tests — H2: fs/read uploads 路径穿越 + token 只存摘要。

审计 H2: ``GET /api/v1/fs/read?path=uploads/../auth.db`` 把根设为 ``~/.veya``,
``uploads/../auth.db`` 合法 → 读走明文 token → 接管任意账号。
修复: 根改 ``~/.veya/uploads`` 并用 ``resolve()+is_relative_to``。

Run: .venv/bin/python -m pytest tests/test_sec_audit_h2_fs_traversal.py -q
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server import auth as auth_mod


@pytest.fixture
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """隔离的 ~/.veya, 里面放一个"敏感" auth.db 和一个真实 uploads/ 文件。"""
    home = tmp_path / "home"
    veya = home / ".veya"
    (veya / "uploads").mkdir(parents=True)
    (veya / "auth.db").write_text("SENSITIVE-TOKENS", encoding="utf-8")
    (veya / "uploads" / "ok.txt").write_text("hello upload", encoding="utf-8")
    monkeypatch.setattr("server.routes.fs.Path.home", lambda: home)
    # 工作区根也隔离, 免读开发者的真实 cwd
    workspace = tmp_path / "ws"
    workspace.mkdir()
    monkeypatch.setenv("VEYA_WORKSPACE", str(workspace))
    return home


@pytest.fixture
def app() -> FastAPI:
    from server.routes.fs import router

    app = FastAPI()
    app.include_router(router)
    app.add_middleware(auth_mod.AuthGateMiddleware)
    return app


@pytest.fixture
def token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
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
    user = auth_mod.create_user("memberuser", "a-very-long-password")
    return auth_mod.issue_token(user["user_id"])


def test_uploads_traversal_blocked(app: FastAPI, fake_home: Path, token: str) -> None:
    client = TestClient(app)
    resp = client.get(
        "/api/v1/fs/read",
        params={"path": "uploads/../auth.db", "max_chars": 200000},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 403, resp.text


def test_uploads_normal_file_ok(app: FastAPI, fake_home: Path, token: str) -> None:
    client = TestClient(app)
    resp = client.get(
        "/api/v1/fs/read",
        params={"path": "uploads/ok.txt"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    assert "hello upload" in resp.json()["content"]


def test_uploads_absolute_escape_blocked(app: FastAPI, fake_home: Path, token: str) -> None:
    client = TestClient(app)
    resp = client.get(
        "/api/v1/fs/read",
        params={"path": "uploads/../../etc/passwd"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 403


def test_fs_requires_auth(app: FastAPI) -> None:
    client = TestClient(app)
    assert client.get("/api/v1/fs/read", params={"path": "uploads/ok.txt"}).status_code == 401


def test_tokens_never_stored_plaintext(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """H2/L1: 库里只落 sha256 摘要; 明文 token 不落盘。"""
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
    user = auth_mod.create_user("memberuser", "a-very-long-password")
    tok = auth_mod.issue_token(user["user_id"])
    with sqlite3.connect(str(db)) as conn:
        stored = {r[0] for r in conn.execute("SELECT token FROM tokens")}
    assert tok not in stored
    assert auth_mod.hash_token(tok) in stored
