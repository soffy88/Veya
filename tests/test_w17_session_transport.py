"""W17 — service.py session execution over SSH transport."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from server.remote_control import approval as approval_mod
from server.remote_control import executor as executor_mod
from server.remote_control import hosts as hosts_mod
from server.remote_control import identity as identity_mod
from server.remote_control import service as service_mod
from server.remote_control import sessions as sessions_mod
from tests.test_w17_ssh_tools import FakeApprovals


@dataclass
class DummyHandle:
    pid: int = 12345
    returncode: int | None = None


class TrackingSpawner:
    def __init__(self) -> None:
        self.spawned: list[list[str]] = []

    def __call__(self, argv, **kwargs):
        self.spawned.append(list(argv))
        return DummyHandle(pid=12345, returncode=None)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("VEYA_REMOTE_CONTROL", "1")
    monkeypatch.setenv("VEYA_SSH_HOSTS", "1")
    monkeypatch.setenv("VEYA_REMOTE_CONTROL_HOSTS_PATH", str(tmp_path / "hosts.json"))
    monkeypatch.setenv("VEYA_REMOTE_CONTROL_POLICY_PATH", str(tmp_path / "policy.json"))
    monkeypatch.setenv("VEYA_REMOTE_CONTROL_SESSIONS_PATH", str(tmp_path / "sessions.json"))
    monkeypatch.setenv("VEYA_SSH_HOSTS_CM_DIR", str(tmp_path / "cm"))

    root = tmp_path / "w"
    root.mkdir()
    host = hosts_mod.RemoteHost(
        host_id="lap",
        display_name="Laptop",
        tailnet_ip="100.64.0.7",
        owner_id="u1",
        write_root=str(root),
        default_cwd=str(root),
        ssh_user="testuser",
        ssh_port=2222,
    )
    hosts_mod.save_host(host)
    identity_mod.save_policy(identity_mod.IdentityPolicy(default="deny", allowlist=["host:laptop"]))
    monkeypatch.setattr(
        service_mod,
        "check_identity",
        lambda host, op: identity_mod.PeerIdentity(
            node_name="laptop", ip=host.tailnet_ip, resolved=True
        ),
    )
    fake = FakeApprovals()
    monkeypatch.setattr(approval_mod, "_service", lambda: fake)
    return {"host": host, "approvals": fake, "root": root}


async def test_session_request_start_defaults_to_ssh_transport(env, monkeypatch):
    """When VEYA_SSH_HOSTS=1, request_start with host_id uses transport='ssh'."""
    res = await service_mod.request_start(
        host_id="lap",
        task="run diagnostics",
        owner_id="u1",
    )
    session = res["session"]
    assert session.transport == "ssh"
    assert session.host_id == "lap"
    assert session.state == sessions_mod.RemoteState.ACCEPTED

    # When flag is OFF, it keeps 'local'
    monkeypatch.setenv("VEYA_SSH_HOSTS", "0")
    res_off = await service_mod.request_start(
        host_id="lap",
        task="run diagnostics",
        owner_id="u1",
    )
    assert res_off["session"].transport == "local"


async def test_session_advance_and_launch_via_ssh_transport(env, monkeypatch):
    """Advancing an approved session with transport='ssh' invokes executor with ssh argv."""
    res = await service_mod.request_start(
        host_id="lap",
        task="build project",
        owner_id="u1",
    )
    session = res["session"]
    aid = res["approval_id"]
    assert session.transport == "ssh"

    # Resolve approval
    env["approvals"].resolve(aid, "once")

    spawner = TrackingSpawner()
    monkeypatch.setattr(executor_mod, "_default_spawner", spawner)

    adv = service_mod.advance(session.session_id, approval_id=aid)
    assert adv["launched"] is True
    assert adv["session"].state == sessions_mod.RemoteState.ACKED

    assert len(spawner.spawned) == 1
    argv = spawner.spawned[0]
    assert argv[0] == "ssh"
    assert "-o" in argv
    assert "BatchMode=yes" in argv
    assert "-p" in argv and "2222" in argv
    assert "testuser@100.64.0.7" in argv
    assert "--" in argv
