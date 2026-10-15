"""SEC audit regression tests — 认证/授权基座 (H1, H2, L1, M1)。

- H1: 默认拒绝 (无/坏 token → 401), ``get_current_user`` 不再回落 anonymous;
      本地单用户模式仅在 loopback 生效。
- H1: ``require_owner`` — 未认证 401, 非 owner 403, owner 放行。
- H2/L1: token 只存 sha256 摘要; 遗留明文 token 首次校验时迁移, 用户不掉登录。

Run: .venv/bin/python -m pytest tests/test_sec_audit_auth.py -q
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from fastapi import Depends, FastAPI, HTTPException
from fastapi.testclient import TestClient

from server import auth as auth_mod


@pytest.fixture
def auth_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolate the auth DB per test (never touch the real ~/.veya/auth.db)."""
    db = tmp_path / "auth.db"
    monkeypatch.setattr(auth_mod, "_DB_PATH", db)
    # Re-create the schema against the temp path.
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
    # Drop the request-local identity so a previous test's user cannot leak.
    auth_mod._user_ctx.set(None)
    monkeypatch.delenv("VEYA_LOCAL_SINGLE_USER", raising=False)
    monkeypatch.delenv("VEYA_OWNER_USER_ID", raising=False)
    monkeypatch.delenv("VEYA_OWNER_USER_IDS", raising=False)
    return db


@pytest.fixture
def owner(auth_db: Path) -> dict:
    user = auth_mod.create_user("owneruser", "a-very-long-password", is_owner=True)
    user["password"] = "a-very-long-password"
    return user


@pytest.fixture
def member(auth_db: Path) -> dict:
    user = auth_mod.create_user("memberuser", "another-long-password", is_owner=False)
    user["password"] = "another-long-password"
    return user


@pytest.fixture
def app() -> FastAPI:
    """Minimal app exercising the auth dependency surface."""
    app = FastAPI()

    @app.get("/public")
    async def public() -> dict:
        return {"ok": True}

    @app.get("/protected")
    async def protected(user: dict = Depends(auth_mod.require_user)) -> dict:
        return {"user_id": user["user_id"]}

    @app.get("/owner-only")
    async def owner_only(user: dict = Depends(auth_mod.require_owner)) -> dict:
        return {"user_id": user["user_id"]}

    @app.get("/whoami")
    async def whoami(user: dict | None = Depends(auth_mod.get_current_user)) -> dict:
        return {"user": user}

    return app


# ── H1: default deny ───────────────────────────────────────────────────


def test_no_token_returns_401(app: FastAPI) -> None:
    client = TestClient(app)
    resp = client.get("/protected")
    assert resp.status_code == 401


def test_invalid_token_returns_401(app: FastAPI) -> None:
    client = TestClient(app)
    resp = client.get("/protected", headers={"Authorization": "Bearer not-a-real-token"})
    assert resp.status_code == 401


def test_public_route_still_open(app: FastAPI) -> None:
    """An explicitly public route keeps working without a token."""
    client = TestClient(app)
    assert client.get("/public").status_code == 200


def test_valid_token_passes(app: FastAPI, member: dict) -> None:
    token = auth_mod.issue_token(member["user_id"])
    client = TestClient(app)
    resp = client.get("/protected", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    assert resp.json()["user_id"] == member["user_id"]


def test_get_current_user_does_not_fall_back_to_anonymous(auth_db: Path) -> None:
    """SEC H1: an absent/invalid token is None, not the anonymous user."""
    assert auth_mod.get_current_user(None, None) is None
    assert auth_mod.get_current_user("Bearer garbage", None) is None


# ── H1: local single-user escape hatch ─────────────────────────────────


def test_local_single_user_rejects_spoofed_forwarded_host(
    app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    """X-Forwarded-Host must NOT grant local single-user access.

    The header is client-controlled, so the loopback decision reads the socket
    peer (``request.client.host``) instead. A TestClient peer is never loopback,
    so local mode stays closed.
    """
    monkeypatch.setenv("VEYA_LOCAL_SINGLE_USER", "1")
    for header in ("X-Forwarded-Host", "Host", "X-Real-IP", "Forwarded"):
        client = TestClient(app)
        resp = client.get("/protected", headers={header: "127.0.0.1"})
        assert resp.status_code == 401, f"{header} must not grant local access"


def test_local_single_user_allows_genuine_loopback(
    app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A request whose socket peer really is loopback is admitted."""
    monkeypatch.setenv("VEYA_LOCAL_SINGLE_USER", "1")
    client = TestClient(app, client=("127.0.0.1", 50000))
    resp = client.get("/whoami")
    assert resp.status_code == 200
    assert resp.json()["user"]["user_id"] == "local"


def test_local_single_user_disabled_by_default(app: FastAPI) -> None:
    client = TestClient(app, client=("127.0.0.1", 50000))
    assert client.get("/protected").status_code == 401


# ── H1: owner gate ────────────────────────────────────────────────────


def test_owner_route_denies_member(app: FastAPI, member: dict) -> None:
    token = auth_mod.issue_token(member["user_id"])
    client = TestClient(app)
    resp = client.get("/owner-only", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 403


def test_owner_route_denies_anonymous(app: FastAPI) -> None:
    client = TestClient(app)
    resp = client.get("/owner-only")
    assert resp.status_code == 401


def test_owner_route_allows_owner(app: FastAPI, owner: dict) -> None:
    token = auth_mod.issue_token(owner["user_id"])
    client = TestClient(app)
    resp = client.get("/owner-only", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200


def test_owner_via_env_id(app: FastAPI, auth_db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """VEYA_OWNER_USER_ID promotes an otherwise ordinary account."""
    user = auth_mod.create_user("promoted", "yet-another-pass")
    monkeypatch.setenv("VEYA_OWNER_USER_ID", user["user_id"])
    token = auth_mod.issue_token(user["user_id"])
    client = TestClient(app)
    assert (
        client.get("/owner-only", headers={"Authorization": f"Bearer {token}"}).status_code == 200
    )


# ── H2/L1: token hashing + migration ───────────────────────────────────


def test_token_is_stored_hashed(auth_db: Path, member: dict) -> None:
    token = auth_mod.issue_token(member["user_id"])
    with sqlite3.connect(str(auth_db)) as conn:
        rows = conn.execute("SELECT token FROM tokens").fetchall()
    stored = [r[0] for r in rows]
    assert token not in stored, "plaintext token must never be persisted"
    assert auth_mod.hash_token(token) in stored


def test_legacy_plaintext_token_migrates_on_first_use(auth_db: Path, member: dict) -> None:
    """A pre-H2 plaintext token keeps working and is upgraded to a hash."""
    plaintext = "legacy-token-value-0123456789abcdef"
    with sqlite3.connect(str(auth_db)) as conn:
        conn.execute(
            "INSERT INTO tokens (token, user_id, created_at, expires_at) VALUES (?,?,?,?)",
            (plaintext, member["user_id"], 1, 2**40),
        )
    # First validation migrates; the caller does not lose the session.
    resolved = auth_mod.resolve_token(plaintext)
    assert resolved is not None
    assert resolved["user_id"] == member["user_id"]
    # Plaintext is gone, digest is present.
    with sqlite3.connect(str(auth_db)) as conn:
        rows = {r[0] for r in conn.execute("SELECT token FROM tokens").fetchall()}
    assert plaintext not in rows
    assert auth_mod.hash_token(plaintext) in rows


def test_database_leak_does_not_forge_sessions(auth_db: Path, member: dict) -> None:
    """H2: only digests are stored, so reading the file yields no usable token."""
    token = auth_mod.issue_token(member["user_id"])
    with sqlite3.connect(str(auth_db)) as conn:
        leaked = {r[0] for r in conn.execute("SELECT token FROM tokens").fetchall()}
    assert token not in leaked
    # A digest is not accepted as a bearer credential.
    assert auth_mod.resolve_token(auth_mod.hash_token(token)) is None


def test_owner_flag_persisted(auth_db: Path, owner: dict, member: dict) -> None:
    users = {u["username"]: u for u in auth_mod.list_users()}
    assert users["owneruser"]["is_owner"] is True
    assert users["memberuser"]["is_owner"] is False


def test_short_password_rejected(auth_db: Path) -> None:
    """L1: raise the password floor instead of accepting 6 characters."""
    with pytest.raises(ValueError):
        auth_mod.create_user("weakuser", "short")
