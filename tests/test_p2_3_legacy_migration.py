import pytest
from fastapi.testclient import TestClient
from server.app import app

client = TestClient(app)

def test_flow_phase1_post():
    res = client.post("/api/v1/flow/phase1", json={"prompt": "test"})
    # Will be 401 (unauthorized) because auth is enforced, or 422/200
    assert res.status_code in (401, 422, 200)

def test_notifications_stream_auth():
    res = client.get("/api/v1/notifications/stream")
    assert res.status_code == 401
