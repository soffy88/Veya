"""W17 REST additions on the W13 router (offline)."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server import auth
from server.remote_control import hosts as hosts_mod
from server.remote_control import identity as identity_mod
from server.remote_control import service as service_mod
from server.remote_control import ssh_tools
from server.routes import remote_control as routes
from tests.w17_fakes import LocalExecRunner


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("VEYA_REMOTE_CONTROL_HOSTS_PATH", str(tmp_path / "hosts.json"))
    monkeypatch.setenv("VEYA_REMOTE_CONTROL_POLICY_PATH", str(tmp_path / "policy.json"))
    monkeypatch.setenv("VEYA_SSH_HOSTS_CM_DIR", str(tmp_path / "cm"))
    monkeypatch.setenv("VEYA_REMOTE_CONTROL", "1")
    monkeypatch.setenv("VEYA_SSH_HOSTS", "1")
    root = tmp_path / "w"
    root.mkdir()
    (root / "f.txt").write_text("x")
    hosts_mod.save_host(
        hosts_mod.RemoteHost(
            host_id="lap",
            display_name="L",
            tailnet_ip="100.64.0.7",
            owner_id="u1",
            write_root=str(root),
        )
    )
    hosts_mod.save_host(
        hosts_mod.RemoteHost(
            host_id="other", display_name="O", tailnet_ip="100.64.0.8", owner_id="u2"
        )
    )
    monkeypatch.setattr(
        service_mod,
        "check_identity",
        lambda host, action: identity_mod.PeerIdentity(node_name="laptop", resolved=True),
    )
    monkeypatch.setattr(ssh_tools, "RUNNER", LocalExecRunner())
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[auth.require_user] = lambda: {"user_id": "u1"}
    app.dependency_overrides[auth.get_current_user] = lambda: {"user_id": "u1"}
    return TestClient(app), root


def test_rest_404_while_ssh_flag_off(client, monkeypatch) -> None:
    c, _ = client
    monkeypatch.delenv("VEYA_SSH_HOSTS", raising=False)
    assert c.get("/api/v1/remote/hosts/lap/probe").status_code == 404
    assert c.get("/api/v1/remote/hosts").status_code == 200  # W13 surface unchanged
    body = c.get("/api/v1/remote/hosts").json()["hosts"][0]
    assert "write_root" in body  # set explicitly → shown
    assert "ssh_port" not in body  # defaults omitted → W13 shape unchanged


def test_rest_probe_owner_and_unknown(client) -> None:
    c, _ = client
    ok = c.get("/api/v1/remote/hosts/lap/probe")
    assert ok.status_code == 200 and ok.json()["reachable"] is True
    for host in ("other", "ghost"):
        assert c.get(f"/api/v1/remote/hosts/{host}/probe").status_code == 404


def test_rest_update_ssh_fields_validated(client) -> None:
    c, root = client
    bad = c.post(
        "/api/v1/remote/hosts/lap/ssh",
        json={"ssh_key_ref": "/home/me/.ssh/id", "write_root": str(root)},
    )
    assert bad.status_code == 422
    good = c.post(
        "/api/v1/remote/hosts/lap/ssh",
        json={"ssh_user": "me", "write_root": str(root), "read_approved": True},
    )
    assert good.status_code == 200 and good.json()["ssh_user"] == "me"
    files = c.get("/api/v1/remote/hosts/lap/files", params={"path": str(root)})
    assert files.status_code == 200 and [e["name"] for e in files.json()["entries"]] == ["f.txt"]
    assert c.post("/api/v1/remote/hosts/other/ssh", json={}).status_code == 404


def test_rest_files_needs_read_opt_in_and_runs(client) -> None:
    c, _ = client
    assert c.get("/api/v1/remote/hosts/lap/files").status_code == 409
    runs = c.get("/api/v1/remote/hosts/lap/runs")
    assert runs.status_code == 200 and runs.json()["runs"] == []
