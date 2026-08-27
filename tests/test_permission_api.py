"""G5: 权限 HTTP API 测试（/permission 端点）。"""

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.external


@pytest.fixture(scope="module")
def client():
    from server.app import app

    with TestClient(app) as c:
        yield c


def test_evaluate_allow_no_prompt(client):
    r = client.post("/permission/evaluate", json={"action": "read", "persona": "build"})
    assert r.status_code == 200
    data = r.json()
    assert data["decision"] == "allow"
    assert data["request_id"] is None


def test_evaluate_deny(client):
    r = client.post("/permission/evaluate", json={"action": "write", "persona": "plan"})
    assert r.json()["decision"] == "deny"


def test_evaluate_pending_returns_request_id(client):
    r = client.post("/permission/evaluate", json={"action": "bash", "persona": "build"})
    data = r.json()
    assert data["decision"] == "pending"
    assert data["request_id"]


def test_pending_list_and_approve_roundtrip(client):
    r = client.post("/permission/evaluate", json={"action": "bash", "persona": "build"})
    rid = r.json()["request_id"]

    pending = client.get("/permission/pending").json()
    assert any(x["request_id"] == rid for x in pending)

    r = client.post(f"/permission/{rid}/approve", json={"note": "approved by test"})
    assert r.status_code == 200
    data = r.json()
    assert data["decision"] == "allow"
    assert data["action"] == "bash"
    assert data["persona"] == "build"

    # 决出后不再挂起
    pending = client.get("/permission/pending").json()
    assert not any(x["request_id"] == rid for x in pending)


def test_deny_roundtrip(client):
    r = client.post("/permission/evaluate", json={"action": "bash", "persona": "build"})
    rid = r.json()["request_id"]
    r = client.post(f"/permission/{rid}/deny", json={"note": "denied by test"})
    assert r.json()["decision"] == "deny"


def test_unknown_request_404(client):
    assert client.post("/permission/nope/approve").status_code == 404
    assert client.post("/permission/nope/deny").status_code == 404


def test_resolve_twice_is_404(client):
    r = client.post("/permission/evaluate", json={"action": "bash", "persona": "build"})
    rid = r.json()["request_id"]
    assert client.post(f"/permission/{rid}/approve").status_code == 200
    assert client.post(f"/permission/{rid}/approve").status_code == 404
