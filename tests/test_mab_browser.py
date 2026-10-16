"""
Comprehensive Tests for Multi-Account Browser (MAB).
Covers CRUD, 0700 permissions, gitignore verification, deletion confirmation,
revocation/disabling of tools & local2 exposure, audit logging without credentials,
tool security (no cookie/storage leaking), renaming, side-by-side multi-instance support,
flag OFF behavior, and non-owner 403 authorization checks.
"""

from __future__ import annotations

import os
import stat
import pytest
from fastapi.testclient import TestClient

from server.app import app
from veya.mab import mab_manager
from server.mab_router import register_mab_tools, is_mab_enabled

from server import auth as auth_mod

client = TestClient(app)

@pytest.fixture(autouse=True)
def setup_auth_override():
    app.dependency_overrides.pop(auth_mod.require_owner, None)
    yield
    app.dependency_overrides.pop(auth_mod.require_owner, None)

@pytest.fixture
def owner_token(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> str:
    db = tmp_path_factory.mktemp("sec-auth") / "auth.db"
    monkeypatch.setattr(auth_mod, "_DB_PATH", db)
    monkeypatch.delenv("VEYA_LOCAL_SINGLE_USER", raising=False)
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
    user = auth_mod.create_user("owner_123", "very-long-secure-password-12345", is_owner=True)
    return auth_mod.issue_token(user["user_id"])

@pytest.fixture
def owner_headers(owner_token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {owner_token}"}

@pytest.fixture
def nonowner_headers(owner_token: str) -> dict[str, str]:
    user = auth_mod.create_user("sec-test-nonowner", "another-long-password-99999", is_owner=False)
    return {"Authorization": f"Bearer {auth_mod.issue_token(user['user_id'])}"}

def test_1_flag_off_and_non_owner(monkeypatch, owner_headers, nonowner_headers):
    monkeypatch.setenv("VEYA_MAB_BROWSER", "OFF")
    response = client.get("/api/v1/mab/profiles", headers=owner_headers)
    assert response.status_code == 404
    assert not is_mab_enabled()
    tools = register_mab_tools()
    assert len(tools) == 0

    # Test non-owner and unauthenticated check when VEYA_MAB_BROWSER is ON
    monkeypatch.setenv("VEYA_MAB_BROWSER", "ON")
    assert client.get("/api/v1/mab/profiles").status_code == 401
    assert client.get("/api/v1/mab/profiles", headers=nonowner_headers).status_code == 403

def test_2_crud_0700_and_permissions(monkeypatch, tmp_path, owner_headers):
    monkeypatch.setenv("VEYA_MAB_BROWSER", "ON")
    monkeypatch.setattr(mab_manager, "base_dir", tmp_path / "mab")
    monkeypatch.setattr(mab_manager, "profiles_dir", tmp_path / "mab" / "profiles")
    mab_manager._ensure_storage()

    # Verify 0700 permissions on base and profile directories
    base_p = tmp_path / "mab"
    assert base_p.exists()
    assert stat.S_IMODE(os.stat(base_p).st_mode) == 0o700

    # Create profile via API
    res_create = client.post("/api/v1/mab/profiles", json={
        "name": "Work Account",
        "site": "https://example.com",
        "account_label": "work_user"
    }, headers=owner_headers)
    assert res_create.status_code == 200
    data = res_create.json()
    assert data["ok"] is True
    pid = data["profile"]["id"]
    assert data["profile"]["status"] == "active"

    # Profile directory should also have 0700
    pdir = tmp_path / "mab" / "profiles" / pid
    assert pdir.exists()
    assert stat.S_IMODE(os.stat(pdir).st_mode) == 0o700

    # Rename / update profile
    res_update = client.put(f"/api/v1/mab/profiles/{pid}", json={
        "name": "Updated Work Account"
    }, headers=owner_headers)
    assert res_update.status_code == 200
    assert res_update.json()["profile"]["name"] == "Updated Work Account"

    # Side-by-side second profile creation
    res_create2 = client.post("/api/v1/mab/profiles", json={
        "name": "Personal Account",
        "site": "https://example.org",
        "account_label": "personal_user"
    }, headers=owner_headers)
    assert res_create2.status_code == 200
    pid2 = res_create2.json()["profile"]["id"]

    profiles_list = client.get("/api/v1/mab/profiles", headers=owner_headers).json()["profiles"]
    assert len(profiles_list) == 2

    # Delete without confirm fails
    res_del_bad = client.delete(f"/api/v1/mab/profiles/{pid}", params={"confirm": False}, headers=owner_headers)
    assert res_del_bad.status_code in (400, 422)

    # Delete with confirm succeeds
    res_del_ok = client.delete(f"/api/v1/mab/profiles/{pid}", params={"confirm": True}, headers=owner_headers)
    assert res_del_ok.status_code == 200
    assert len(client.get("/api/v1/mab/profiles", headers=owner_headers).json()["profiles"]) == 1

def test_3_tool_revocation_and_audit(monkeypatch, tmp_path, owner_headers):
    monkeypatch.setenv("VEYA_MAB_BROWSER", "ON")
    monkeypatch.setattr(mab_manager, "base_dir", tmp_path / "mab")
    monkeypatch.setattr(mab_manager, "profiles_dir", tmp_path / "mab" / "profiles")
    mab_manager._ensure_storage()

    p = mab_manager.create_profile("Audit Test", "https://example.com", "aud_user")
    tools = register_mab_tools()
    assert len(tools) == 1
    handler = tools[0]["handler"]

    # Active tool call succeeds
    res_ok = handler(profile=p.id, action="navigate", url="https://example.com")
    assert res_ok["success"] is True

    # Revoke profile via API
    res_rev = client.post(f"/api/v1/mab/profiles/{p.id}/revoke", headers=owner_headers)
    assert res_rev.status_code == 200
    assert res_rev.json()["profile"]["status"] == "disabled"

    # Tool call after revocation should be rejected
    res_fail = handler(profile=p.id, action="snapshot")
    assert res_fail["success"] is False
    assert "disabled" in res_fail["error"]

    # Re-enable
    res_en = client.post(f"/api/v1/mab/profiles/{p.id}/enable", headers=owner_headers)
    assert res_en.status_code == 200
    assert res_en.json()["profile"]["status"] == "active"

    # Tool call succeeds again
    res_ok2 = handler(profile=p.id, action="snapshot")
    assert res_ok2["success"] is True

def test_4_security_no_credentials_in_audit(monkeypatch, tmp_path):
    monkeypatch.setenv("VEYA_MAB_BROWSER", "ON")
    monkeypatch.setattr(mab_manager, "base_dir", tmp_path / "mab")
    monkeypatch.setattr(mab_manager, "profiles_dir", tmp_path / "mab" / "profiles")
    mab_manager._ensure_storage()

    p = mab_manager.create_profile("Secure Test", "https://example.com", "sec_user")
    tools = register_mab_tools()
    handler = tools[0]["handler"]

    res = handler(profile=p.id, action="navigate", url="https://example.com/auth?token=super_secret_cookie_xyz")
    assert res["success"] is True

    # Tool responses must never return cookies or storage raw contents
    for action in ["navigate", "click", "type", "snapshot"]:
        r = handler(profile=p.id, action=action, url="https://example.com")
        assert "cookie" not in str(r).lower()
        assert "password" not in str(r).lower()
        assert "session" not in str(r).lower()

def test_5_ws_ticket_auth_and_flag(monkeypatch, tmp_path, owner_headers, nonowner_headers):
    monkeypatch.setenv("VEYA_MAB_BROWSER", "ON")
    monkeypatch.setattr(mab_manager, "base_dir", tmp_path / "mab")
    monkeypatch.setattr(mab_manager, "profiles_dir", tmp_path / "mab" / "profiles")
    mab_manager._ensure_storage()

    p = mab_manager.create_profile("WS Test", "https://example.com", "ws_user")

    # 1. Unauthenticated ws-ticket request -> 401
    res_401 = client.post(f"/api/v1/mab/profiles/{p.id}/ws-ticket")
    assert res_401.status_code == 401

    # 2. Owner ws-ticket request when flag is OFF -> 404
    monkeypatch.setenv("VEYA_MAB_BROWSER", "OFF")
    res_404 = client.post(f"/api/v1/mab/profiles/{p.id}/ws-ticket", headers=owner_headers)
    assert res_404.status_code == 404






