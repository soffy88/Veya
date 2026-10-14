"""W17 — remote background runs → W13 verifier evidence (offline)."""

from __future__ import annotations

import json
import time

import pytest

from server.remote_control import approval as approval_mod
from server.remote_control import hosts as hosts_mod
from server.remote_control import identity as identity_mod
from server.remote_control import ssh_tools, tools
from server.remote_control.sessions import evidence_from_bg_status

from tests.test_w17_ssh_tools import FakeApprovals
from tests.w17_fakes import LocalExecRunner


def test_bg_status_evidence_shape() -> None:
    done = evidence_from_bg_status({"host": "lap", "name": "j", "finished": True, "exit_code": 0,
                                    "log_tail": "a\nall tests passed\n"})
    assert done.usable and done.source == "ssh_bg:lap:j"
    assert done.summary == "exit=0 tail=all tests passed"
    assert done.detail["exit_code"] == 0 and done.detail["finished"] is True
    failed = evidence_from_bg_status({"host": "lap", "name": "j", "finished": True, "exit_code": 2, "log_tail": "x"})
    assert not failed.usable and failed.summary.startswith("exit=2")
    running = evidence_from_bg_status({"host": "lap", "name": "j", "finished": False, "exit_code": None})
    assert not running.usable and running.summary == ""
    lost = evidence_from_bg_status({"host": "lap", "name": "j", "finished": True, "exit_code": None, "lost": True})
    assert not lost.usable and lost.detail["lost"] is True


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("VEYA_REMOTE_CONTROL_HOSTS_PATH", str(tmp_path / "hosts.json"))
    monkeypatch.setenv("VEYA_REMOTE_CONTROL_POLICY_PATH", str(tmp_path / "policy.json"))
    monkeypatch.setenv("VEYA_SSH_HOSTS_CONSUMED_PATH", str(tmp_path / "consumed.json"))
    monkeypatch.setenv("VEYA_SSH_HOSTS_CM_DIR", str(tmp_path / "cm"))
    root = tmp_path / "w"
    root.mkdir()
    hosts_mod.save_host(hosts_mod.RemoteHost(host_id="lap", display_name="Laptop", tailnet_ip="100.64.0.7",
                                             owner_id="u1", write_root=str(root)))
    identity_mod.save_policy(identity_mod.IdentityPolicy(default="deny", allowlist=["host:laptop"]))
    monkeypatch.setattr(ssh_tools, "IDENTITY_RESOLVER",
                        lambda ip: identity_mod.PeerIdentity(node_name="laptop", ip=ip, resolved=True))
    monkeypatch.setattr(ssh_tools, "RUNNER", LocalExecRunner())
    fake = FakeApprovals()
    monkeypatch.setattr(approval_mod, "_service", lambda: fake)
    monkeypatch.setattr(tools, "_caller_owner_id", lambda: "u1")
    return {"root": root, "approvals": fake}


async def test_bg_run_lifecycle_produces_usable_evidence(env) -> None:
    kwargs = {"host_id": "lap", "name": "build", "command": "echo step1; sleep 0.3; echo finished-ok"}
    pending = json.loads(await ssh_tools.ssh_bg_start(**kwargs, approved=True))
    env["approvals"].resolve(pending["approval_id"], "once")
    started = json.loads(await ssh_tools.ssh_bg_start(**kwargs, approved=True, approval_id=pending["approval_id"]))
    assert started["log_path"].endswith("/.veya/run/build.log")
    status = json.loads(await ssh_tools.ssh_bg_status(host_id="lap", name="build"))
    deadline = time.time() + 10
    while not status["finished"] and time.time() < deadline:
        time.sleep(0.1)
        status = json.loads(await ssh_tools.ssh_bg_status(host_id="lap", name="build"))
    assert status["exit_code"] == 0 and status["verified"] is True
    assert status["evidence"]["source"] == "ssh_bg:lap:build"
    assert status["evidence"]["summary"] == "exit=0 tail=finished-ok"


def test_evidence_feeds_existing_mark_done(tmp_path, monkeypatch) -> None:
    """The adapter's evidence is accepted by the W13 session state machine."""
    from server.remote_control import sessions

    sig = sessions.mark_done.__code__.co_varnames
    assert "evidence" in sig
    ev = evidence_from_bg_status({"host": "lap", "name": "j", "finished": True, "exit_code": 0, "log_tail": "ok"})
    assert sessions.VerifierEvidence.from_dict(ev.to_dict()).usable
