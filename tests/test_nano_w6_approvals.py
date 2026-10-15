"""NANO-W6: tiered approvals + cross-device @runner routing with approval relay.

Covers: oskill permission evaluation, oprim approval atoms, omodul approval
flow, the gated pause/resume in user_control, always-grants (list/revoke),
human handoff (Done/Cancel), guard L (agent principals, high-risk human-only),
the push relay, and @runner mention routing.
"""

from __future__ import annotations

import asyncio
import os
import time

import pytest

pytestmark = pytest.mark.asyncio


@pytest.fixture()
def approval_store(tmp_path):
    from veya.obase.approvals import ApprovalStore

    return ApprovalStore(str(tmp_path / "approvals.db"))


@pytest.fixture()
def wired(approval_store, monkeypatch):
    """Gate flag on + service bound to a temp store + a captured push relay."""

    from server import approvals as approvals_mod
    from server.notification_center import global_notifier

    monkeypatch.setenv("VEYA_APPROVAL_GATES", "1")
    approvals_mod.configure_service(store=approval_store)
    approvals_mod.session_grants.clear()
    pushes: list[dict] = []
    observer = lambda message: pushes.append(message)  # noqa: E731
    global_notifier.add_push_observer(observer)
    yield approval_store, pushes
    global_notifier.remove_push_observer(observer)
    approvals_mod.session_grants.clear()


@pytest.fixture()
def user_ctx(monkeypatch):
    from server import auth as auth_mod
    from server import approvals as approvals_mod
    from server import user_control as uc

    tok = auth_mod._user_ctx.set({"user_id": "user-1", "username": "user-1"})
    sid = uc._session_id.set("session-1")
    yield {"user_id": "user-1"}
    uc._session_id.reset(sid)
    auth_mod._user_ctx.reset(tok)


# ── oskill pure evaluation ────────────────────────────────────────────


def test_pure_evaluate_grant_allow_and_gated_ask():
    from veya.oskill.pure.permission import evaluate_capability_permission

    grants = [{"tool": "delete_file", "scope_pattern": ""}]
    assert (
        evaluate_capability_permission(
            tool="delete_file", args={}, grants=grants, flags={"scope": "abc"}
        )
        == "allow"
    )
    assert (
        evaluate_capability_permission(
            tool="send_email", args={}, grants=[], flags={"scope": "abc"}
        )
        == "ask"
    )


def test_pure_evaluate_human_only_and_deny():
    from veya.oskill.pure.permission import evaluate_capability_permission

    grants = [{"tool": "db_migration", "scope_pattern": ""}]
    assert (
        evaluate_capability_permission(
            tool="db_migration", args={}, grants=grants, flags={"scope": "abc"}
        )
        == "ask"
    )
    assert (
        evaluate_capability_permission(
            tool="read_file", args={}, grants=[], flags={"scope": "abc", "deny": ["read_file"]}
        )
        == "deny"
    )


def test_composite_decision_allow_ask_deny(approval_store):
    from veya.oskill.approval import request_permission_decision

    allow = request_permission_decision(
        tool="delete_file", args={}, scope="abc", store=approval_store
    )
    assert allow["decision"] == "ask"
    approval_store.put_grant(scope="abc", tool="delete_file")
    allow = request_permission_decision(
        tool="delete_file", args={}, scope="abc", store=approval_store
    )
    assert allow["decision"] == "allow"
    assert allow["grant"] is not None
    deny = request_permission_decision(
        tool="read_file",
        args={},
        scope="abc",
        flags={"deny": ["read_file"]},
        store=approval_store,
    )
    assert deny["decision"] == "deny"


# ── oprim atoms ───────────────────────────────────────────────────────


def test_oprim_approval_and_grant_atoms(approval_store):
    from veya.oprim import approval_create, approval_resolve, grant_list, grant_lookup, grant_put, grant_revoke

    row = approval_create(
        {"request_id": "r1", "scope": "abc", "tool": "delete_file", "tool_args": {"path": "/x"}},
        store=approval_store,
    )
    assert row["status"] == "pending"
    # idempotent create
    again = approval_create(
        {"request_id": "r1", "scope": "abc", "tool": "delete_file"}, store=approval_store
    )
    assert again["status"] == "pending"

    grant = grant_put(scope="abc", tool="delete_file", store=approval_store)
    assert grant_lookup(scope="abc", tool="delete_file", store=approval_store)["id"] == grant["id"]
    assert len(grant_list(scope="abc", store=approval_store)) == 1
    assert grant_revoke(grant["id"], store=approval_store) is True
    assert grant_lookup(scope="abc", tool="delete_file", store=approval_store) is None

    resolved = approval_resolve("r1", decision="once", reason="ok", store=approval_store)
    assert resolved["decision"] == "once"
    # second resolve is a no-op (CAS)
    assert approval_resolve("r1", decision="deny", store=approval_store) is None


def test_oprim_handoff_atoms(approval_store):
    from veya.oprim import handoff_create, handoff_resolve

    row = handoff_create({"id": "h1", "scope": "abc", "reason": "login"}, store=approval_store)
    assert row["status"] == "pending"
    done = handoff_resolve("h1", status="done", store=approval_store)
    assert done["status"] == "done"
    assert handoff_resolve("h1", status="cancelled", store=approval_store) is None


# ── omodul approval flow ───────────────────────────────────────────────


async def test_omodul_approval_flow_request_resolve(approval_store):
    from veya.omodul.approval_flow import ApprovalFlowConfig, run_approval_flow

    trails: list[tuple[str, dict]] = []
    config = ApprovalFlowConfig(
        store=approval_store,
        trail=lambda topic, payload: trails.append((topic, payload)),
        scope="abc",
    )
    result = await run_approval_flow(
        config, {"op": "request", "tool": "delete_file", "tool_args": {"path": "/x"}}, "/tmp"
    )
    assert result["status"] == "completed"
    assert result["approval"]["status"] == "pending"
    resolved = await run_approval_flow(
        config,
        {"op": "resolve", "approval_id": result["approval_id"], "decision": "always"},
        "/tmp",
    )
    assert resolved["status"] == "completed"
    assert resolved["approval"]["decision"] == "always"
    topics = [topic for topic, _ in trails]
    assert "approval.requested" in topics and "approval.resolved" in topics


async def test_omodul_approval_flow_never_raises(approval_store):
    from veya.omodul.approval_flow import ApprovalFlowConfig, run_approval_flow

    config = ApprovalFlowConfig(store=approval_store)
    result = await run_approval_flow(config, {"op": "nonsense"}, "/tmp")
    assert result["status"] == "error"
    result = await run_approval_flow(
        config, {"op": "resolve", "approval_id": "missing", "decision": "once"}, "/tmp"
    )
    assert result["status"] == "error"


async def test_omodul_handoff_flow(approval_store):
    from veya.omodul.approval_flow import ApprovalFlowConfig, run_approval_flow

    config = ApprovalFlowConfig(store=approval_store, scope="abc")
    requested = await run_approval_flow(
        config, {"op": "handoff_request", "reason": "captcha"}, "/tmp"
    )
    assert requested["status"] == "completed"
    done = await run_approval_flow(
        config, {"op": "handoff_done", "handoff_id": requested["handoff_id"]}, "/tmp"
    )
    assert done["status"] == "completed"
    assert done["handoff"]["status"] == "done"


# ── gated pause/resume in user_control ─────────────────────────────────


async def test_gated_approval_pauses_and_resumes(wired, user_ctx):
    from server import approvals as approvals_mod
    from server import user_control as uc

    store, pushes = wired
    task = asyncio.create_task(uc._wait_approval("delete_file", {"path": "/tmp/x"}))
    await asyncio.sleep(0.2)
    assert not task.done()
    pending = store.list_approvals(scope=approvals_mod.get_approval_service().scope_for("user-1"))
    assert len(pending) == 1
    # the relay fired a push carrying the deep link
    assert any(
        (m.get("payload") or {}).get("approval_id") == pending[0]["request_id"] for m in pushes
    )
    await approvals_mod.get_approval_service().resolve(
        approval_id=pending[0]["request_id"], decision="once", principal="human"
    )
    assert await task is None


async def test_gated_approval_deny(wired, user_ctx):
    from server import approvals as approvals_mod
    from server import user_control as uc

    store, _ = wired
    task = asyncio.create_task(uc._wait_approval("send_email", {"to": "a@b.c"}))
    await asyncio.sleep(0.2)
    pending = store.list_approvals(scope=approvals_mod.get_approval_service().scope_for("user-1"))
    await approvals_mod.get_approval_service().resolve(
        approval_id=pending[0]["request_id"], decision="deny", principal="human"
    )
    result = await task
    assert result is not None and "denied" in result


async def test_gated_approval_always_grant_skips_pause(wired, user_ctx):
    from server import approvals as approvals_mod
    from server import user_control as uc

    store, pushes = wired
    store.put_grant(
        scope=approvals_mod.get_approval_service().scope_for("user-1"), tool="delete_file"
    )
    assert await uc._wait_approval("delete_file", {"path": "/tmp/y"}) is None
    assert pushes == []


async def test_gated_approval_always_decision_persists_grant(wired, user_ctx):
    from server import approvals as approvals_mod
    from server import user_control as uc

    store, _ = wired
    task = asyncio.create_task(uc._wait_approval("delete_file", {"path": "/tmp/z"}))
    await asyncio.sleep(0.2)
    pending = store.list_approvals(scope=approvals_mod.get_approval_service().scope_for("user-1"))
    await approvals_mod.get_approval_service().resolve(
        approval_id=pending[0]["request_id"], decision="always", principal="human"
    )
    assert await task is None
    grants = store.list_grants(scope=approvals_mod.get_approval_service().scope_for("user-1"))
    assert any(g["tool"] == "delete_file" for g in grants)
    # and the grant now lets the same tool through without a pause
    assert await uc._wait_approval("delete_file", {"path": "/tmp/w"}) is None


async def test_gated_approval_session_grant(wired, user_ctx):
    from server import approvals as approvals_mod
    from server import user_control as uc

    store, _ = wired
    task = asyncio.create_task(uc._wait_approval("send_message", {"text": "hi"}))
    await asyncio.sleep(0.2)
    pending = store.list_approvals(scope=approvals_mod.get_approval_service().scope_for("user-1"))
    await approvals_mod.get_approval_service().resolve(
        approval_id=pending[0]["request_id"], decision="session", principal="human"
    )
    assert await task is None
    # session grant: same tool+scope now allowed without a new pause
    assert await uc._wait_approval("send_message", {"text": "again"}) is None
    # but a different tool still pauses
    task2 = asyncio.create_task(uc._wait_approval("send_email", {"to": "x@y.z"}))
    await asyncio.sleep(0.2)
    assert not task2.done()
    # list_approvals is newest-first and includes resolved rows: pick the pending one
    pending2 = store.list_approvals(
        scope=approvals_mod.get_approval_service().scope_for("user-1"), status="pending"
    )
    assert len(pending2) == 1 and pending2[0]["tool"] == "send_email"
    await approvals_mod.get_approval_service().resolve(
        approval_id=pending2[0]["request_id"], decision="deny", principal="human"
    )
    assert await asyncio.wait_for(task2, timeout=5) is not None


async def test_handoff_done_and_cancel(wired, user_ctx):
    from server import approvals as approvals_mod
    from server import user_control as uc

    store, _ = wired
    service = approvals_mod.get_approval_service()
    task = asyncio.create_task(uc.request_handoff("complete login"))
    await asyncio.sleep(0.2)
    assert not task.done()
    pending = _pending_handoffs(store)
    assert len(pending) == 1
    await service.resume_handoff(handoff_id=pending[0]["id"], done=True)
    assert await task is None

    task2 = asyncio.create_task(uc.request_handoff("enter captcha"))
    await asyncio.sleep(0.2)
    pending2 = _pending_handoffs(store)
    await service.resume_handoff(handoff_id=pending2[0]["id"], done=False)
    assert "cancelled" in await task2


def _pending_handoffs(store):
    """All pending handoffs for the wired store (test helper)."""

    return store.list_handoffs(status="pending")


# ── guard L ────────────────────────────────────────────────────────────


async def test_guard_l_agent_cannot_approve(approval_store):
    from fastapi import HTTPException

    from server import approvals as approvals_mod

    service = approvals_mod.configure_service(store=approval_store)
    requested = await service.request(
        tool="delete_file", args={"path": "/x"}, user_id="user-1"
    )
    with pytest.raises(HTTPException) as exc:
        await service.resolve(
            approval_id=requested["approval_id"], decision="once", principal="agent:chef"
        )
    assert exc.value.status_code == 403


async def test_guard_l_delegated_agent_may_approve(approval_store):
    from server import approvals as approvals_mod

    service = approvals_mod.configure_service(store=approval_store)
    requested = await service.request(
        tool="delete_file",
        args={"path": "/x"},
        user_id="user-1",
        delegate_policy="allow:agent:chef",
    )
    result = await service.resolve(
        approval_id=requested["approval_id"], decision="once", principal="agent:chef"
    )
    assert result["status"] == "completed"


async def test_guard_l_high_risk_human_only(approval_store):
    from fastapi import HTTPException

    from server import approvals as approvals_mod

    service = approvals_mod.configure_service(store=approval_store)
    requested = await service.request(
        tool="db_migration",
        args={"db": "prod"},
        user_id="user-1",
        delegate_policy="allow:agent:chef",
    )
    with pytest.raises(HTTPException) as exc:
        await service.resolve(
            approval_id=requested["approval_id"], decision="once", principal="agent:chef"
        )
    assert exc.value.status_code == 403
    # a human may still resolve it
    result = await service.resolve(
        approval_id=requested["approval_id"], decision="once", principal="human"
    )
    assert result["status"] == "completed"


async def test_guard_l_double_resolve_conflict(approval_store):
    from fastapi import HTTPException

    from server import approvals as approvals_mod

    service = approvals_mod.configure_service(store=approval_store)
    requested = await service.request(tool="delete_file", args={}, user_id="user-1")
    await service.resolve(
        approval_id=requested["approval_id"], decision="once", principal="human"
    )
    with pytest.raises(HTTPException) as exc:
        await service.resolve(
            approval_id=requested["approval_id"], decision="deny", principal="human"
        )
    assert exc.value.status_code == 409


# ── REST: list / revoke grants, resolve endpoint ───────────────────────


def _rest_client():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from server import auth as auth_mod
    from server.approvals import router

    app = FastAPI()
    app.dependency_overrides[auth_mod.require_user] = lambda: {
        "user_id": "anonymous", "username": "test-anonymous", "is_owner": False
    }
    app.include_router(router)
    return TestClient(app)


def test_rest_grants_list_and_revoke(approval_store, monkeypatch):
    from server import approvals as approvals_mod

    approvals_mod.configure_service(store=approval_store)
    store = approval_store
    store.put_grant(scope=approvals_mod.ApprovalService.scope_for("user-1"), tool="delete_file")
    store.put_grant(scope=approvals_mod.ApprovalService.scope_for("user-1"), tool="send_email")
    store.put_grant(scope=approvals_mod.ApprovalService.scope_for("user-2"), tool="delete_file")
    client = _rest_client()
    # no token → anonymous scope: isolated from user-1's grants
    resp = client.get("/api/v1/grants")
    assert resp.status_code == 200
    assert resp.json()["grants"] == []
    # user-1's token: needs a valid bearer — auth falls back to anonymous for
    # unknown tokens, so drive the service directly for scope assertions.
    user1_grants = store.list_grants(scope=approvals_mod.ApprovalService.scope_for("user-1"))
    assert len(user1_grants) == 2
    assert store.revoke_grant(user1_grants[0]["id"]) is True
    assert len(store.list_grants(scope=approvals_mod.ApprovalService.scope_for("user-1"))) == 1
    # user-2's grant is untouched (per-user isolation)
    assert len(store.list_grants(scope=approvals_mod.ApprovalService.scope_for("user-2"))) == 1


async def test_rest_approvals_list_isolated(approval_store):
    from server import approvals as approvals_mod

    service = approvals_mod.configure_service(store=approval_store)
    await service.request(tool="delete_file", args={}, user_id="user-1")
    rows = service.list_pending(scope=service.scope_for("user-1"))
    assert len(rows) == 1
    assert service.list_pending(scope=service.scope_for("user-2")) == []


# ── @runner routing ────────────────────────────────────────────────────


def test_mention_parsing_pure():
    from veya.oskill.pure.mention import parse_runner_mention

    assert parse_runner_mention("@Home do the thing") == {
        "name": "home",
        "rest": "do the thing",
    }
    assert parse_runner_mention("@home") == {"name": "home", "rest": ""}
    assert parse_runner_mention("hello @home") is None
    assert parse_runner_mention("@") is None
    assert parse_runner_mention("") is None


def test_route_text_to_runner_match_and_unknown(tmp_path):
    from server.bot_team.mention import route_text_to_runner
    from server.bot_team.runners import RunnerRegistry

    registry = RunnerRegistry(storage_path=tmp_path / "runners.json")
    registry.add(runner_id="home", name="Home Server", base_url="http://100.64.0.5:8765", token_ref="VEYA_T_HOME")
    registry.add(runner_id="work", name="workstation", base_url="http://100.64.0.6:8765", token_ref="VEYA_T_WORK")

    # case-insensitive, matches ids and display names
    assert route_text_to_runner("@home do something", registry=registry)["runner_id"] == "home"
    assert route_text_to_runner("@HOME SERVER do something", registry=registry)["runner_id"] == "home"
    assert route_text_to_runner("@Work fix it", registry=registry)["runner_id"] == "work"
    # not a mention → None
    assert route_text_to_runner("hello", registry=registry) is None
    # unknown name → helpful error listing runners
    missing = route_text_to_runner("@nope hello", registry=registry)
    assert missing["error"] == "unknown runner 'nope'"
    names = {view["runner_id"] for view in missing["runners"]}
    assert {"local", "home", "work"} <= names


async def test_run_bot_turn_unknown_mention_errors_visibly(tmp_path, monkeypatch):
    from server.bot_team.turns import run_bot_turn
    from server.goal_run.bot_identity import BotSpec

    # an empty registry: every mention is unknown
    monkeypatch.setenv("VEYA_RUNNERS_PATH", str(tmp_path / "runners.json"))
    result = await run_bot_turn(
        bot=BotSpec(bot_id="b1", owner_id="o", name="b", instructions="", provider="", model=""),
        text="@nope hello",
        user={"user_id": "u1"},
    )
    assert result.status == "error"
    assert "nope" in result.reply
    assert "已注册" in result.reply


async def test_run_bot_turn_mention_forwards_to_runner(tmp_path, monkeypatch):
    import json

    from server.bot_team import runner_client as rc
    from server.bot_team.turns import run_bot_turn
    from server.goal_run.bot_identity import BotSpec

    runners_file = tmp_path / "runners.json"
    runners_file.write_text(
        json.dumps(
            {
                "home": {
                    "runner_id": "home",
                    "name": "Home",
                    "base_url": "http://100.64.0.5:8765",
                    "token_ref": "VEYA_T_HOME",
                    "status": "unknown",
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("VEYA_RUNNERS_PATH", str(runners_file))
    monkeypatch.setenv("VEYA_T_HOME", "secret-token")

    calls: list[tuple[str, str]] = []

    async def _fake_forward(*, bot, text, user, context="", workspace_id=None, trace_id=None, conversations=None, client=None, registry=None):
        calls.append((text, registry))
        from server.bot_team.runner_client import BotTurnResult

        return BotTurnResult(
            bot_id=bot.bot_id,
            task_id="t-1",
            session_id="s-1",
            reply="done on runner",
            status="completed",
            trace_id=trace_id or "",
        )

    monkeypatch.setattr(rc, "forward_bot_turn", _fake_forward)
    result = await run_bot_turn(
        bot=BotSpec(bot_id="b1", owner_id="o", name="b", instructions="", provider="", model=""),
        text="@home please fix the bug",
        user={"user_id": "u1"},
    )
    assert result.status == "completed"
    assert result.reply == "done on runner"
    assert calls[0][0] == "please fix the bug"


# ── flag default off ───────────────────────────────────────────────────


def test_flag_off_by_default(monkeypatch):
    from server import approvals as approvals_mod

    monkeypatch.delenv("VEYA_APPROVAL_GATES", raising=False)
    assert approvals_mod.approval_gates_enabled() is False
    monkeypatch.setenv("VEYA_APPROVAL_GATES", "1")
    assert approvals_mod.approval_gates_enabled() is True


async def test_rest_resolve_rejects_other_users_approval(approval_store):
    """A signed-in user cannot answer another account's approval (404, still pending)."""

    from server import approvals as approvals_mod

    service = approvals_mod.configure_service(store=approval_store)
    record = await service.request(tool="delete_file", args={}, user_id="user-1")
    rid = record["approval_id"]
    client = _rest_client()  # no token -> anonymous account
    resp = client.post(f"/api/v1/approvals/{rid}/resolve", json={"decision": "once"})
    assert resp.status_code == 404
    assert service.get(rid)["status"] == "pending"
