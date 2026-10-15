import pytest
from fastapi.testclient import TestClient

from server import auth as auth_mod
from server.app import app


@pytest.fixture
def client_with_real_users(tmp_path, monkeypatch):
    monkeypatch.setenv("VEYA_GSK_EVENT_ROOT", str(tmp_path))
    monkeypatch.setattr(auth_mod, "_DB_PATH", tmp_path / "auth.db")
    auth_mod._init_db()
    client = TestClient(app)

    owner = client.post(
        "/api/v1/auth/register",
        json={"username": "gsk_owner_test", "password": "local-test-pass-1"},
    )
    member = client.post(
        "/api/v1/auth/register",
        json={"username": "gsk_member_test", "password": "local-test-pass-2"},
    )
    assert owner.status_code == 200, owner.text
    assert member.status_code == 200, member.text
    return client, owner.json(), member.json()


def test_gsk_routes_feature_flags_are_hidden_when_off(client_with_real_users, monkeypatch):
    client, owner, _ = client_with_real_users
    monkeypatch.setenv("VEYA_OWNER_USER_ID", owner["user_id"])
    monkeypatch.delenv("VEYA_GSK_EGRESS_RECEIPTS", raising=False)
    monkeypatch.delenv("VEYA_GSK_EVIDENCE", raising=False)

    assert client.get("/api/v1/gsk/receipts").status_code == 404
    assert client.get("/api/v1/gsk/evidence", params={"label": "missing"}).status_code == 404


def test_gsk_receipts_requires_real_session_and_owner(client_with_real_users, monkeypatch, tmp_path):
    client, owner, member = client_with_real_users
    monkeypatch.setenv("VEYA_GSK_EGRESS_RECEIPTS", "1")
    monkeypatch.setenv("VEYA_GSK_EGRESS_RECEIPTS_PATH", str(tmp_path / "receipts.jsonl"))
    monkeypatch.setenv("VEYA_OWNER_USER_ID", owner["user_id"])

    assert client.get("/api/v1/gsk/receipts").status_code == 401
    assert client.get(
        "/api/v1/gsk/receipts",
        headers={"Authorization": f"Bearer {member['token']}"},
    ).status_code == 403
    response = client.get(
        "/api/v1/gsk/receipts",
        headers={"Authorization": f"Bearer {owner['token']}"},
    )
    assert response.status_code == 200, response.text
    assert "receipts" in response.json()
    assert "verification" in response.json()


def test_gsk_evidence_requires_real_session_and_owner(client_with_real_users, monkeypatch, tmp_path):
    client, owner, member = client_with_real_users
    monkeypatch.setenv("VEYA_GSK_EVIDENCE", "1")
    monkeypatch.setenv("VEYA_GSK_EVIDENCE_PATH", str(tmp_path / "evidence.jsonl"))
    monkeypatch.setenv("VEYA_OWNER_USER_ID", owner["user_id"])

    assert client.get("/api/v1/gsk/evidence", params={"label": "missing"}).status_code == 401
    assert client.get(
        "/api/v1/gsk/evidence",
        params={"label": "missing"},
        headers={"Authorization": f"Bearer {member['token']}"},
    ).status_code == 403
    response = client.get(
        "/api/v1/gsk/evidence",
        params={"label": "missing"},
        headers={"Authorization": f"Bearer {owner['token']}"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "MISSING"


def test_spoofed_identity_headers_do_not_grant_gsk_access(client_with_real_users, monkeypatch):
    client, owner, _ = client_with_real_users
    monkeypatch.setenv("VEYA_GSK_EGRESS_RECEIPTS", "1")
    monkeypatch.setenv("VEYA_OWNER_USER_ID", owner["user_id"])
    response = client.get(
        "/api/v1/gsk/receipts",
        headers={"X-User-ID": owner["user_id"], "X-Is-Admin": "true"},
    )
    assert response.status_code == 401
