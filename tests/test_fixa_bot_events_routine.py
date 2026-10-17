"""TDD tests for FIXA:
1. POST /api/v1/bots preserves and persists available_skill_ids on creation.
2. Event trigger dispatcher for message/signal rules (group & bot chat messages + /signals).
3. Routine creation defaults enabled=True, while respecting explicit enabled=False.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server import auth as auth_mod
from server.auth import AuthGateMiddleware
from server.goal_run.bot_identity import BotRegistry, BotSpec
from server.goal_run.models import RoutineSpec
from server.goal_run.routine_history import RoutineHistoryStore
from server.goal_run.routine_registry import RoutineRegistry
from server.routes.bot_team import router as bot_team_router
from server.routes.nano_w9 import router as nano_w9_router
from server.routes.routines import router as routines_router

USER = {"user_id": "fixa-user", "username": "fixa"}


@pytest.fixture
def fixa_env(tmp_path, monkeypatch):
    bot_root = tmp_path / "bots"
    reg_path = tmp_path / "bot_registry.json"
    routines_path = tmp_path / "routines.json"
    history_dir = tmp_path / "routine_history"

    monkeypatch.setenv("VEYA_BOTS_ROOT", str(bot_root))
    monkeypatch.setenv("VEYA_BOT_REGISTRY_PATH", str(reg_path))
    monkeypatch.setenv("VEYA_ROUTINE_REGISTRY_PATH", str(routines_path))
    monkeypatch.setenv("VEYA_ROUTINE_HISTORY_DIR", str(history_dir))
    monkeypatch.setenv("VEYA_EVENT_TRIGGERS", "1")
    monkeypatch.setenv("VEYA_ROUTINE_TRIGGERS", "1")

    return {
        "bot_root": bot_root,
        "reg_path": reg_path,
        "routines_path": routines_path,
        "history_dir": history_dir,
    }


@pytest.fixture
def test_app(fixa_env):
    app = FastAPI()
    app.add_middleware(AuthGateMiddleware)

    # Dependency overrides for testing auth
    app.dependency_overrides[auth_mod.require_user] = lambda: USER
    app.dependency_overrides[auth_mod.get_current_user] = lambda: USER

    app.include_router(bot_team_router)
    app.include_router(routines_router)
    app.include_router(nano_w9_router)
    from server.routes.nano_w9 import signals_compat_router
    app.include_router(signals_compat_router)

    from server.routes import routines as _routines_mod
    app.dependency_overrides[_routines_mod._require_owned_bot] = lambda: None

    return app


@pytest.fixture
def client(test_app, owner_headers):
    c = TestClient(test_app)
    c.headers.update(owner_headers)
    return c


# ── Issue 1: POST 创建 Bot 时传 available_skill_ids ────────────────────────


def test_create_bot_persists_available_skill_ids(client, fixa_env, monkeypatch):
    """POST /api/v1/bots must persist available_skill_ids immediately upon creation."""
    bot_id = "test_researcher"
    skills = ["agency_design_ui_designer", "preset:test-driven-development"]

    resp = client.post(
        "/api/v1/bots",
        json={
            "bot_id": bot_id,
            "name": "Researcher",
            "description": "Research bot",
            "instructions": "Be rigorous.",
            "available_skill_ids": skills,
        },
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert "bot" in data
    # 1. Returned view must contain the skills passed in POST
    assert data["bot"]["available_skill_ids"] == skills

    # 2. Persisted BotSpec in registry must contain the skills
    spec = BotRegistry().get(bot_id)
    assert spec is not None
    assert spec.available_skill_ids == skills

    # 3. GET /api/v1/bots/{bot_id} must also return the skills
    get_resp = client.get(f"/api/v1/bots/{bot_id}")
    assert get_resp.status_code == 200
    assert get_resp.json()["bot"]["available_skill_ids"] == skills

    # 4. Under VEYA_ROLE_SKILLS_RUNTIME=1, build_objective immediately injects skills
    monkeypatch.setenv("VEYA_ROLE_SKILLS_RUNTIME", "1")
    from server.bot_team.turns import build_objective

    prompt = build_objective(bot=spec, text="find evidence")
    assert "<available_skills>" in prompt
    assert "test-driven-development" in prompt


# ── Issue 2: 消息规则信号分发器与群/私聊消息真实匹配与派发 ──────────────────


def test_signal_endpoint_message_pattern_matching_and_dispatch(client, fixa_env):
    """POST /api/v1/nano-w9/signals must match pattern/text and dispatch via a real dispatcher."""
    registry = RoutineRegistry(bot_id="default-bot")
    routine_id = "r-msg-dispatch-1"
    spec = RoutineSpec(
        routine_id=routine_id,
        trigger_topic="event.arrive",
        objective="Handle deploy message",
        goal_template="Handle deploy message",
        enabled=True,
        bot_id="default-bot",
        triggers=[{"kind": "message", "pattern": "deploy.*prod"}],
    )
    registry.register(spec)

    # Post signal with pattern/source
    resp = client.post(
        "/api/v1/nano-w9/signals",
        json={"kind": "message", "source": "group", "pattern": "deploy to prod now"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()

    assert data["status"] == "ok"
    assert len(data["dispatched"]) >= 1
    assert data["dispatched"][0]["routine_id"] == routine_id

    # Verify history recorded
    history_store = RoutineHistoryStore(bot_id="default-bot")
    records = history_store.get_history(routine_id)
    assert len(records) >= 1
    assert records[0]["status"] == "dispatched"

    # Also test the /api/v1/signals alias endpoint
    resp_alias = client.post(
        "/api/v1/signals",
        json={"kind": "message", "source": "group", "pattern": "deploy to prod now"},
    )
    assert resp_alias.status_code == 200, resp_alias.text
    assert resp_alias.json()["status"] == "ok"


def test_group_and_bot_messages_match_rules_and_dispatch_routine(client, fixa_env, monkeypatch):
    """Group chat messages and bot messages must match message rules and trigger routines."""
    # Register routine with message trigger for a specific pattern
    registry = RoutineRegistry(bot_id="fixa-bot")
    routine_id = "r-group-alert"
    spec = RoutineSpec(
        routine_id=routine_id,
        trigger_topic="event.arrive",
        objective="Trigger alert on incident",
        goal_template="Trigger alert on incident",
        enabled=True,
        bot_id="fixa-bot",
        triggers=[{"kind": "message", "regex": "INCIDENT-\\d+"}],
    )
    registry.register(spec)

    # Register member bots
    bot_reg = BotRegistry()
    bot_reg.register(BotSpec(bot_id="fixa-bot", owner_id=USER["user_id"], name="Fixa Bot"))
    bot_reg.register(BotSpec(bot_id="helper-bot", owner_id=USER["user_id"], name="Helper Bot"))

    # Create a group
    grp_resp = client.post(
        "/api/v1/bots/groups",
        json={"name": "Ops War Room", "bot_ids": ["fixa-bot", "helper-bot"]},
    )
    assert grp_resp.status_code == 200
    group_id = grp_resp.json()["group"]["id"]

    # Fake master execution to avoid LLM network calls
    async def fake_execute(*args, **kwargs):
        from server.goal_run.models import GoalRunResponse, GoalStatus
        return GoalRunResponse(goal_id="g1", status=GoalStatus.completed, phase="finalized", summary="ack")

    from server.coordinator_master import master_coordinator
    monkeypatch.setattr(master_coordinator, "execute_structured", fake_execute)

    # Post group message containing INCIDENT-99
    msg_resp = client.post(
        f"/api/v1/bots/groups/{group_id}/messages",
        json={"text": "Attention team: INCIDENT-99 is active!"},
    )
    assert msg_resp.status_code == 200, msg_resp.text

    # Verify that the message rule triggered the routine
    history_store = RoutineHistoryStore(bot_id="fixa-bot")
    records = history_store.get_history(routine_id)
    assert len(records) >= 1
    assert records[0]["status"] == "dispatched"

    # Post 1:1 private bot message containing INCIDENT-42
    bot_msg_resp = client.post(
        "/api/v1/bots/fixa-bot/messages",
        json={"text": "Alert: INCIDENT-42 occurred"},
    )
    assert bot_msg_resp.status_code == 200, bot_msg_resp.text

    # Verify private message also triggered routine
    records_after = history_store.get_history(routine_id)
    assert len(records_after) >= 2
    assert records_after[1]["status"] == "dispatched"


# ── Issue 3: routine 创建默认 enabled=True，且显式传 False 时尊重 ────────────


def test_routine_create_defaults_enabled_true_and_respects_explicit_false(client, fixa_env):
    """POST /api/v1/bots/{bot_id}/routines must default enabled=True and respect enabled=False."""
    bot_id = "default-bot"

    # 1. Omit enabled -> should default to True
    resp1 = client.post(
        f"/api/v1/bots/{bot_id}/routines",
        json={
            "objective": "Hourly security check",
            "schedule": "every 1h",
        },
    )
    assert resp1.status_code == 200, resp1.text
    data1 = resp1.json()
    assert data1["enabled"] is True
    # Verify in registry
    spec1 = RoutineRegistry(bot_id=bot_id).get(data1["routine_id"])
    assert spec1 is not None
    assert spec1.enabled is True

    # 2. Explicitly pass enabled=False -> should respect False
    resp2 = client.post(
        f"/api/v1/bots/{bot_id}/routines",
        json={
            "objective": "Draft monthly report",
            "schedule": "every 30d",
            "enabled": False,
        },
    )
    assert resp2.status_code == 200, resp2.text
    data2 = resp2.json()
    assert data2["enabled"] is False
    # Verify in registry
    spec2 = RoutineRegistry(bot_id=bot_id).get(data2["routine_id"])
    assert spec2 is not None
    assert spec2.enabled is False
