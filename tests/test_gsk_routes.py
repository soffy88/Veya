import pytest
from fastapi.testclient import TestClient
from server.app import app

def test_gsk_routes_owner_auth(monkeypatch):
    monkeypatch.setenv("VEYA_OWNER_USER_ID", "owner_123")
    client = TestClient(app)

    response = client.get("/api/v1/gsk/receipts", headers={"X-User-ID": "intruder"})
    assert response.status_code == 403

    response_ok = client.get("/api/v1/gsk/receipts", headers={"X-User-ID": "owner_123"})
    assert response_ok.status_code == 200
    data = response_ok.json()
    assert "receipts" in data
    assert "verification" in data
