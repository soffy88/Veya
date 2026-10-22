from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from server.app import app
from server.auth import _connect, _init_db
from veya.rkz import routines

client = TestClient(app)


@pytest.fixture(autouse=True)
def setup_env(tmp_path, monkeypatch):
    routines_dir = tmp_path / "routines"
    routines_dir.mkdir()
    monkeypatch.setenv("VEYA_RKZ_ROUTINES_DIR", str(routines_dir))
    monkeypatch.setenv("VEYA_RKZ_ROUTINES", "1")
    monkeypatch.setenv("VEYA_LOCAL_SINGLE_USER", "1")
    # Setup test owner token in auth db
    _init_db()
    with _connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO users (id, username, password_hash, created_at, is_owner) VALUES (?, ?, ?, ?, ?)",
            ("owner1", "owner", "fakehash", 1234567890, 1),
        )
        conn.execute(
            "INSERT OR REPLACE INTO tokens (token, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
            ("test-owner-token", "owner1", 1234567890, 9999999999),
        )
        conn.execute(
            "INSERT OR REPLACE INTO users (id, username, password_hash, created_at, is_owner) VALUES (?, ?, ?, ?, ?)",
            ("user1", "regular", "fakehash", 1234567890, 0),
        )
        conn.execute(
            "INSERT OR REPLACE INTO tokens (token, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
            ("test-regular-token", "user1", 1234567890, 9999999999),
        )
    yield


def test_routines_crud_and_validation(tmp_path):
    valid_content = """---
name: my-routine-1
description: Test routine description
schedule: "0 0 * * *"
requires_approval: true
tools:
  - web_search
  - file_read
---
# Routine Steps
1. Step one
2. Step two
"""

    # 1. Save / Create via routines module
    meta = routines.save_routine(valid_content)
    assert meta["name"] == "my-routine-1"
    assert meta["requires_approval"] is True

    # 2. Get routine
    data = routines.get_routine("my-routine-1")
    assert data["meta"]["name"] == "my-routine-1"
    assert "Step one" in data["body"]

    # 3. List routines
    lst = routines.list_routines()
    assert len(lst) == 1
    assert lst[0]["name"] == "my-routine-1"

    # 4. Path traversal / invalid name rejection
    with pytest.raises(ValueError):
        routines._get_file_path("../evil")
    with pytest.raises(ValueError):
        routines._get_file_path("has space")

    # 5. Delete without confirm should fail
    with pytest.raises(ValueError):
        routines.delete_routine("my-routine-1", confirm=False)

    # Delete with confirm=True
    assert routines.delete_routine("my-routine-1", confirm=True) is True
    assert len(routines.list_routines()) == 0


def test_api_unauthorized_and_forbidden():
    # 401 without token
    resp = client.get("/api/v1/rkz/routines")
    assert resp.status_code == 401

    # 403 with non-owner token
    resp = client.get(
        "/api/v1/rkz/routines", headers={"Authorization": "Bearer test-regular-token"}
    )
    assert resp.status_code == 403


def test_api_feature_flag_off(monkeypatch):
    monkeypatch.setenv("VEYA_RKZ_ROUTINES", "0")
    resp = client.get("/api/v1/rkz/routines", headers={"Authorization": "Bearer test-owner-token"})
    assert resp.status_code == 404
    # P3-3: a disabled feature is machine-distinguishable from a missing route.
    detail = resp.json()["detail"]
    assert detail["code"] == "feature_disabled"
    assert detail["feature"] == "VEYA_RKZ_ROUTINES"


def test_api_endpoints_success():
    headers = {"Authorization": "Bearer test-owner-token"}
    routine_yaml = """---
name: api-routine
description: API test
schedule: "0 12 * * *"
requires_approval: false
tools:
  - shell_run
---
Run shell command.
"""

    # Create
    resp = client.post("/api/v1/rkz/routines", content=routine_yaml, headers=headers)
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"

    # List
    resp = client.get("/api/v1/rkz/routines", headers=headers)
    assert resp.status_code == 200
    assert len(resp.json()["routines"]) == 1

    # Get
    resp = client.get("/api/v1/rkz/routines/api-routine", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["routine"]["name"] == "api-routine"

    # Update
    updated_yaml = """---
name: api-routine
description: Updated description
schedule: "0 12 * * *"
requires_approval: true
tools:
  - shell_run
---
Updated body.
"""
    resp = client.put("/api/v1/rkz/routines/api-routine", content=updated_yaml, headers=headers)
    assert resp.status_code == 200
    assert resp.json()["routine"]["requires_approval"] is True

    # Delete without confirm -> 400
    resp = client.delete("/api/v1/rkz/routines/api-routine", headers=headers)
    assert resp.status_code == 400

    # Delete with confirm query param
    resp = client.delete("/api/v1/rkz/routines/api-routine?confirm=true", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"
