"""Device link: devices, presence, scopes, versioned envelopes."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server import auth as _device_auth
from server.device_link import (
    MAX_FRAME_BYTES,
    DeviceLinkStore,
    build_envelope,
    parse_envelope,
)
from server.routes.device_link import router as device_link_router

USER = {"user_id": "device-user", "username": "device"}


def test_store_devices_and_presence(tmp_path):
    store = DeviceLinkStore(tmp_path / "links.json")
    device = store.register_device("desk1", USER["user_id"], label="Desktop", platform="linux")
    assert device.last_seen_at == device.created_at
    with pytest.raises(ValueError, match="already exists"):
        store.register_device("desk1", USER["user_id"])
    with pytest.raises(ValueError, match="invalid device_id"):
        store.register_device("bad id!", USER["user_id"])
    touched = store.touch("desk1", USER["user_id"])
    assert touched.last_seen_at >= device.last_seen_at
    with pytest.raises(KeyError):
        store.touch("ghost", USER["user_id"])
    with pytest.raises(PermissionError):
        store.touch("desk1", "someone-else")
    assert [d.device_id for d in store.list_devices(USER["user_id"])] == ["desk1"]
    assert store.list_devices("someone-else") == []


def test_store_scopes_require_owned_distinct_devices(tmp_path):
    store = DeviceLinkStore(tmp_path / "links.json")
    store.register_device("desk1", USER["user_id"])
    store.register_device("phone1", USER["user_id"])
    scope = store.create_scope(
        "provider-share", USER["user_id"], host_device="desk1", guest_device="phone1", ref="openai"
    )
    assert scope.status == "active"
    with pytest.raises(ValueError):
        store.create_scope("provider-share", USER["user_id"], host_device="desk1", guest_device="desk1", ref="x")
    with pytest.raises(KeyError):
        store.create_scope("provider-share", USER["user_id"], host_device="desk1", guest_device="ghost", ref="x")
    with pytest.raises(ValueError):
        store.create_scope("nope", USER["user_id"], host_device="desk1", guest_device="phone1", ref="x")
    assert store.revoke_scope(scope.scope_id, USER["user_id"]) is True
    assert store.revoke_scope("ghost", USER["user_id"]) is False


def test_envelope_version_and_opaque_payload():
    payload = {"nested": {"deep": [1, 2, 3]}, "text": "opaque"}
    env = build_envelope(kind="shared-task", src="desk1", dst="phone1", payload=payload)
    assert env["v"] == 1
    assert parse_envelope(env) == env
    assert env["payload"] == payload
    with pytest.raises(ValueError):
        parse_envelope({**env, "v": 2})
    with pytest.raises(ValueError):
        parse_envelope({"v": 1, "kind": "x", "id": "i", "src": "a"})
    with pytest.raises(ValueError):
        build_envelope(kind="x", src="a", dst="b", payload="y" * (MAX_FRAME_BYTES + 1))


@pytest.fixture
def device_client(tmp_path, monkeypatch):
    monkeypatch.setenv("VEYA_DEVICE_LINK_PATH", str(tmp_path / "links.json"))
    app = FastAPI()
    app.dependency_overrides[_device_auth.require_user] = lambda: dict(USER)
    app.include_router(device_link_router)
    return TestClient(app)


def test_device_link_api(device_client, tmp_path, monkeypatch):
    created = device_client.post(
        "/api/v1/device-link/devices",
        json={"device_id": "desk1", "label": "Desktop", "platform": "linux"},
    )
    assert created.status_code == 200, created.text
    dup = device_client.post("/api/v1/device-link/devices", json={"device_id": "desk1"})
    assert dup.status_code == 409
    device_client.post("/api/v1/device-link/devices", json={"device_id": "phone1"})

    touched = device_client.post("/api/v1/device-link/devices/desk1/touch")
    assert touched.status_code == 200
    assert device_client.post("/api/v1/device-link/devices/ghost/touch").status_code == 404

    share = device_client.post(
        "/api/v1/device-link/provider-shares",
        json={"host_device": "desk1", "guest_device": "phone1", "ref": "openai"},
    )
    assert share.status_code == 200, share.text
    assert share.json()["scope"]["kind"] == "provider-share"

    task = device_client.post(
        "/api/v1/device-link/shared-tasks",
        json={"host_device": "desk1", "guest_device": "phone1", "ref": "task-1"},
    )
    assert task.status_code == 200
    assert task.json()["scope"]["kind"] == "shared-task"

    scopes = device_client.get("/api/v1/device-link/scopes")
    assert len(scopes.json()["scopes"]) == 2
    revoked = device_client.post(
        f"/api/v1/device-link/scopes/{share.json()['scope']['scope_id']}/revoke"
    )
    assert revoked.status_code == 200

    # Ownership on envelopes: a device owned by someone else is 403.
    store = DeviceLinkStore(tmp_path / "links.json")
    store.register_device("other1", "someone-else")
    foreign = device_client.post(
        "/api/v1/device-link/envelopes",
        json={"kind": "shared-task", "src": "desk1", "dst": "other1", "payload": {"x": 1}},
    )
    assert foreign.status_code == 403
    unknown = device_client.post(
        "/api/v1/device-link/envelopes",
        json={"kind": "shared-task", "src": "desk1", "dst": "ghost", "payload": {"x": 1}},
    )
    assert unknown.status_code == 404
    ok = device_client.post(
        "/api/v1/device-link/envelopes",
        json={"kind": "shared-task", "src": "desk1", "dst": "phone1", "payload": {"x": 1}},
    )
    assert ok.status_code == 200
    assert ok.json()["envelope"]["v"] == 1
