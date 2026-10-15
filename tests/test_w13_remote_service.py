"""W13 remote control — service orchestration, REST routes and tools.

Offline: the identity seam is a fake peer, the executor is a fake launcher, and
the approval store is a tmp SQLite file. No network, no real process.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.conftest import ROOT  # noqa: F401 — path bootstrap

LEDGERS = ("HOSTS", "SESSIONS", "WAKE", "FILES", "POLICY")


@pytest.fixture
def rc_env(monkeypatch, tmp_path):
    """Flag on; ledgers, approval store, routines and bots all isolated."""
    monkeypatch.setenv("VEYA_REMOTE_CONTROL", "1")
    for name in LEDGERS:
        monkeypatch.setenv(f"VEYA_REMOTE_CONTROL_{name}_PATH", str(tmp_path / f"{name}.json"))
    monkeypatch.setenv("VEYA_ROUTINE_REGISTRY_PATH", str(tmp_path / "routines.json"))
    monkeypatch.setenv("VEYA_BOT_REGISTRY_PATH", str(tmp_path / "bots.json"))

    from server import approvals
    from veya.obase.approvals import ApprovalStore

    approvals.configure_service(store=ApprovalStore(path=str(tmp_path / "approvals.db")))

    # Identity seam: every peer resolves to a trusted node.
    from server.remote_control import identity

    monkeypatch.setattr(
        identity,
        "resolve_identity",
        lambda ip, runner=None: identity.PeerIdentity(
            node_name="ubuntu26",
            fqdn="ubuntu26.ts.net",
            user_login="alice",
            ip=ip,
            resolved=True,
        ),
    )
    identity.save_policy(identity.IdentityPolicy(default="deny", allowlist=["ubuntu26"]))

    # Executor seam: never fork a process.
    from server.remote_control import executor

    def fake_launch(host, *, task, workspace="", transport="local", spawner=None):
        return executor.LaunchResult(
            host_id=host.host_id,
            agent_label=host.agent_label,
            argv=("pi", "--run", task),
            pid=4242,
            returncode=None,
            transport=transport,
        )

    monkeypatch.setattr(executor, "launch", fake_launch)

    from server.remote_control import hosts

    hosts.save_host(
        hosts.RemoteHost(
            host_id="ubuntu26",
            display_name="Ubuntu",
            tailnet_ip="100.64.0.1",
            owner_id="u1",
            agent_label="pi",
            workspace_path=str(tmp_path),
        )
    )
    hosts.save_host(
        hosts.RemoteHost(
            host_id="other-host",
            display_name="Other",
            tailnet_ip="100.64.0.2",
            owner_id="u1",
            agent_label="codex",
        )
    )
    yield tmp_path
    approvals.configure_service(store=ApprovalStore(path=str(tmp_path / "reset.db")))


async def _approve(approval_id: str) -> None:
    from server import approvals

    await approvals.get_approval_service().resolve(
        approval_id=approval_id, decision="once", principal="human"
    )


async def _start(owner="u1", host="ubuntu26", task="do x", wake="now"):
    from server.remote_control import service

    return await service.request_start(host_id=host, owner_id=owner, task=task, requested_wake=wake)


# --------------------------------------------------------------------------
# Service orchestration
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_records_accepted_session_and_pending_approval(rc_env):
    result = await _start()
    session = result["session"]
    assert session.state == "accepted"
    assert result["approval_id"]

    from server.remote_control import approval

    status, _decision = approval.approval_state(result["approval_id"])
    assert status == "pending"
    # nothing ran: no pid, no argv
    assert session.pid is None
    assert session.argv == []


@pytest.mark.asyncio
async def test_apply_refuses_without_approval(rc_env):
    from server.remote_control import approval, service

    result = await _start()
    with pytest.raises(approval.RemoteApprovalRequired):
        service.apply_approved(result["session"].session_id)


@pytest.mark.asyncio
async def test_apply_after_approval_launches_and_acks(rc_env):
    from server.remote_control import service

    result = await _start()
    await _approve(result["approval_id"])
    applied = service.apply_approved(
        result["session"].session_id, approval_id=result["approval_id"]
    )
    session = applied["session"]
    assert session.state == "acked"
    assert session.pid == 4242
    assert applied["launched"] is True
    # host last_seen was stamped
    from server.remote_control import hosts

    assert hosts.get_host("ubuntu26").last_seen_at is not None


@pytest.mark.asyncio
async def test_apply_refused_when_approval_denied(rc_env):
    from server import approvals
    from server.remote_control import approval, service

    result = await _start()
    await approvals.get_approval_service().resolve(
        approval_id=result["approval_id"], decision="deny", principal="human"
    )
    with pytest.raises(approval.RemoteApprovalRequired):
        service.apply_approved(result["session"].session_id, approval_id=result["approval_id"])


@pytest.mark.asyncio
async def test_cancel_requires_approval_and_only_when_running(rc_env):
    from server.remote_control import service

    result = await _start()
    await _approve(result["approval_id"])
    service.apply_approved(result["session"].session_id, approval_id=result["approval_id"])

    requested = await service.request_cancel(result["session"].session_id, owner_id="u1")
    await _approve(requested["approval_id"])
    cancelled = service.cancel(result["session"].session_id, approval_id=requested["approval_id"])
    assert cancelled.state == "cancelled"


@pytest.mark.asyncio
async def test_handoff_moves_context_to_new_accepted_session(rc_env):
    from server.remote_control import service, sessions

    result = await _start()
    await _approve(result["approval_id"])
    service.apply_approved(result["session"].session_id, approval_id=result["approval_id"])
    sessions.mark_done(
        result["session"].session_id,
        evidence=sessions.VerifierEvidence(verified=True, source="v", summary="ok"),
    )

    # hand off from a fresh accepted session (done is terminal)
    second = await _start(task="carry me")
    await _approve(second["approval_id"])
    service.apply_approved(second["session"].session_id, approval_id=second["approval_id"])
    requested = await service.request_handoff(
        session_id=second["session"].session_id,
        owner_id="u1",
        target_host_id="other-host",
    )
    await _approve(requested["approval_id"])
    source = service.handoff(second["session"].session_id, approval_id=requested["approval_id"])
    assert source.state == "handed_off"

    created = sessions.get_session(source.handoff_to)
    assert created.state == "accepted"
    assert created.host_id == "other-host"
    assert created.context["task"] == "carry me"
    assert created.handoff_from == second["session"].session_id


@pytest.mark.asyncio
async def test_handoff_target_is_fixed_at_request_time(rc_env):
    from server.remote_control import service, sessions

    result = await _start()
    await _approve(result["approval_id"])
    service.apply_approved(result["session"].session_id, approval_id=result["approval_id"])
    requested = await service.request_handoff(
        session_id=result["session"].session_id, owner_id="u1", target_host_id="other-host"
    )
    await _approve(requested["approval_id"])
    # The applied target is the one the approver saw ("other-host"); handoff()
    # takes no target argument, so a caller cannot redirect it.
    source = service.handoff(result["session"].session_id, approval_id=requested["approval_id"])
    assert sessions.get_session(source.handoff_to).host_id == "other-host"


@pytest.mark.asyncio
async def test_complete_requires_evidence_and_notifies(rc_env, monkeypatch):
    from server import notification_center
    from server.remote_control import service, sessions

    pushed: list[str] = []
    monkeypatch.setattr(
        notification_center.global_notifier,
        "push",
        lambda *a, **k: (pushed.append(k.get("user_id", "")), "mid")[1],
    )

    result = await _start()
    await _approve(result["approval_id"])
    service.apply_approved(result["session"].session_id, approval_id=result["approval_id"])

    with pytest.raises(sessions.TransitionRejected):
        service.complete(result["session"].session_id, evidence=sessions.VerifierEvidence())

    done = service.complete(
        result["session"].session_id,
        evidence=sessions.VerifierEvidence(verified=True, source="v", summary="ok"),
    )
    assert done.state == "done"
    # the terminal notify is per-user, never broadcast
    assert pushed and set(pushed) == {"u1"}


# --------------------------------------------------------------------------
# Tools (§8.4)
# --------------------------------------------------------------------------


def test_tools_registered_only_when_flag_on(monkeypatch):
    from server.remote_control import tools
    from server.tool_registry import MasterToolRegistry

    monkeypatch.setenv("VEYA_REMOTE_CONTROL", "0")
    assert tools.register_tools(MasterToolRegistry()) == 0

    monkeypatch.setenv("VEYA_REMOTE_CONTROL", "1")
    registry = MasterToolRegistry()
    assert tools.register_tools(registry) == 5
    assert registry.has("remote_agent_start")
    assert registry.has("remote_task_status")


@pytest.mark.asyncio
async def test_tool_start_refuses_without_approved(rc_env, monkeypatch):
    from server.remote_control import tools

    monkeypatch.setattr(tools, "_caller_owner_id", lambda: "u1")
    out = await tools.remote_agent_start(host_id="ubuntu26", task="x", approved=False)
    assert "approved" in out

    from server.remote_control import sessions

    assert sessions.list_sessions(owner_id="u1") == []


@pytest.mark.asyncio
async def test_tool_start_creates_session_when_approved(rc_env, monkeypatch):
    from server.remote_control import sessions, tools

    monkeypatch.setattr(tools, "_caller_owner_id", lambda: "u1")
    out = await tools.remote_agent_start(host_id="ubuntu26", task="x", approved=True)
    assert "session_id" in out
    assert len(sessions.list_sessions(owner_id="u1")) == 1


@pytest.mark.asyncio
async def test_tool_refuses_without_caller_identity(rc_env):
    from server.remote_control import tools

    # no bot bound in this context → owner is ""
    out = await tools.remote_agent_start(host_id="ubuntu26", task="x", approved=True)
    assert "拒绝" in out


# --------------------------------------------------------------------------
# REST routes (§12)
# --------------------------------------------------------------------------


@pytest.fixture
def client(rc_env):
    from server import auth
    from server.routes.remote_control import router

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[auth.require_user] = lambda: {"user_id": "u1"}
    app.dependency_overrides[auth.get_current_user] = lambda: {"user_id": "u1"}
    return TestClient(app)


def test_routes_404_when_flag_off(client, monkeypatch):
    monkeypatch.setenv("VEYA_REMOTE_CONTROL", "0")
    assert client.get("/api/v1/remote/hosts").status_code == 404


def test_hosts_crud_over_rest(client):
    created = client.post(
        "/api/v1/remote/hosts",
        json={"host_id": "newbox", "tailnet_ip": "100.64.0.9", "agent_label": "pi"},
    )
    assert created.status_code == 201
    assert created.json()["owner_id"] == "u1"

    listing = client.get("/api/v1/remote/hosts").json()["hosts"]
    assert any(h["host_id"] == "newbox" for h in listing)

    assert client.delete("/api/v1/remote/hosts/newbox").status_code == 200
    assert client.delete("/api/v1/remote/hosts/newbox").status_code == 404


def test_session_start_and_status_over_rest(client):
    started = client.post(
        "/api/v1/remote/sessions",
        json={"host_id": "ubuntu26", "task": "hello", "requested_wake": "now"},
    )
    assert started.status_code == 202
    body = started.json()
    assert body["session"]["state"] == "accepted"
    assert body["approval_id"]

    sid = body["session"]["session_id"]
    status = client.get(f"/api/v1/remote/sessions/{sid}")
    assert status.status_code == 200
    assert status.json()["state"] == "accepted"


def test_other_users_session_is_404_not_403(client, monkeypatch):
    from server.remote_control import sessions

    sessions.create_session(host_id="ubuntu26", agent_label="pi", task="x", owner_id="someone-else")
    other = next(s for s in sessions.list_sessions() if s.owner_id == "someone-else")
    resp = client.get(f"/api/v1/remote/sessions/{other.session_id}")
    assert resp.status_code == 404


def test_file_stage_and_fetch_over_rest(client):
    import hashlib

    payload = b"payload-bytes"
    digest = hashlib.sha256(payload).hexdigest()
    staged = client.post(
        "/api/v1/remote/files",
        json={
            "session_id": "s1",
            "filename": "a.txt",
            "size_bytes": len(payload),
            "sha256": digest,
        },
    )
    assert staged.status_code == 201
    token = staged.json()["token"]
    # staged_path never leaves the server
    assert "staged_path" not in staged.json()

    fetched = client.get(f"/api/v1/remote/files/{token}")
    assert fetched.status_code == 200
    assert fetched.json()["sha256"] == digest


def test_wake_policy_put_requires_owned_sender(client):
    ok = client.put(
        "/api/v1/remote/wake-policy",
        json={"sender": "ubuntu26", "agent_label": "pi", "ceiling": "batch"},
    )
    assert ok.status_code == 200
    assert ok.json()["ceiling"] == "batch"

    denied = client.put(
        "/api/v1/remote/wake-policy",
        json={"sender": "not-mine", "agent_label": "pi", "ceiling": "now"},
    )
    assert denied.status_code == 404


@pytest.mark.asyncio
async def test_batched_wake_materialises_routine_and_advances(rc_env, monkeypatch):
    """A `batch` wake registers a real routine; when it fires, the held
    session is launched and the routine is disarmed."""
    from server.goal_run.routine_registry import RoutineRegistry
    from server.remote_control import service, sessions, wake

    wake.set_policy(wake.WakePolicy(sender="ubuntu26", agent_label="pi", ceiling="batch"))
    result = await _start(wake="now")  # requested now, ceiling downgrades to batch
    await _approve(result["approval_id"])
    applied = service.apply_approved(
        result["session"].session_id, approval_id=result["approval_id"], bot_id="u1"
    )
    assert applied["session"].state == "woken"
    routine_id = applied["session"].routine_id
    assert routine_id.startswith("remote-wake-")
    assert RoutineRegistry(bot_id="u1").get(routine_id) is not None

    launched = await service.advance_batched(routine_id=routine_id, bot_id="u1")
    assert launched == [result["session"].session_id]
    assert sessions.get_session(result["session"].session_id).state == "acked"
    # the routine is disarmed after its one window
    assert RoutineRegistry(bot_id="u1").get(routine_id).enabled is False


@pytest.mark.asyncio
async def test_routine_clock_seam_ignores_non_remote_routines(rc_env):
    from server.bot_team import routine_clock

    # a routine that is not a W13 batched wake is never intercepted
    assert (
        await routine_clock._try_remote_wake_handoff(routine_id="supervise_x", bot_id="u1") is False
    )
    # a remote-wake routine *is* intercepted (returns True once handled)
    assert (
        await routine_clock._try_remote_wake_handoff(routine_id="remote-wake-x", bot_id="u1")
        is True
    )


@pytest.mark.asyncio
async def test_routine_clock_seam_off_when_flag_disabled(rc_env, monkeypatch):
    from server.bot_team import routine_clock

    monkeypatch.setenv("VEYA_REMOTE_CONTROL", "0")
    assert (
        await routine_clock._try_remote_wake_handoff(routine_id="remote-wake-x", bot_id="u1")
        is False
    )
