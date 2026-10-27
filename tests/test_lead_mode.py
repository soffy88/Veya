"""Lead reply mode for group chats.

A group with reply_mode="lead" runs one cycle per user message instead of
peer rounds: the admin bot plans and delegates, the other members report,
then the admin synthesizes one final conclusion. A worker that has nothing
to add passes; the lead synthesis still runs.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server.bot_team.conversations import PASS_SENTINEL, ConversationStore
from server.bot_team.groups import LEAD_REPLY_MODE, GroupStore, run_group_turn
from server.events import EventStore
from server.goal_run.bot_identity import BotRegistry, BotSpec
from server.goal_run.models import GoalRunResponse, GoalStatus
from server.routes import product as product_routes
from server.routes.bot_team import router as bots_router
from server.task_store import TaskStore

USER = {"user_id": "lead-user", "username": "lead"}


@pytest.fixture
def lead_env(tmp_path, monkeypatch):
    """Every bot file lands in tmp_path; nothing touches the real ~/.veya."""

    bot_root = tmp_path / "bots-root"
    monkeypatch.setenv("VEYA_BOTS_ROOT", str(bot_root))
    monkeypatch.setenv("VEYA_BOT_REGISTRY_PATH", str(tmp_path / "bot_registry.json"))
    events = EventStore(tmp_path / "events.jsonl")
    tasks = TaskStore(tmp_path / "tasks.json", event_store=events)
    monkeypatch.setattr(product_routes, "task_store", tasks)

    class _History:
        async def save(self, session_id: str, messages: list[Any], *, user_id: str) -> None:
            return None

    monkeypatch.setattr(product_routes, "default_history_store", lambda: _History())
    return {"root": bot_root}


@pytest.fixture
def fake_master(monkeypatch):
    """Deterministic reply per bot: 'say:<bot id>'."""

    async def fake_execute(request: Any) -> GoalRunResponse:
        bot_id = str(request.constraints.metadata.get("bot_id") or "")
        return GoalRunResponse(
            goal_id=f"goal-{bot_id}",
            status=GoalStatus.completed,
            phase="finalized",
            summary=f"say:{bot_id}",
        )

    from server.coordinator_master import master_coordinator

    monkeypatch.setattr(master_coordinator, "execute_structured", fake_execute)


@pytest.fixture
def client():
    app = FastAPI()
    from server import auth as _w11_auth

    app.dependency_overrides[_w11_auth.require_user] = lambda: dict(USER)
    app.include_router(bots_router)
    return TestClient(app)


def _register(registry: BotRegistry, *bot_ids: str) -> BotRegistry:
    for bot_id in bot_ids:
        registry.register(
            BotSpec(
                bot_id=bot_id,
                owner_id=USER["user_id"],
                name=bot_id.capitalize(),
                description=f"Does {bot_id} work.",
                instructions=f"You only do {bot_id} work.",
            )
        )
    return registry


@pytest.mark.asyncio
async def test_lead_cycle_order_plan_report_synthesis(lead_env, fake_master):
    registry = _register(BotRegistry(), "alpha", "beta")
    group = GroupStore(lead_env["root"]).create(
        name="lead-room",
        bot_ids=["alpha", "beta"],
        admin_bot_id="alpha",
        reply_mode=LEAD_REPLY_MODE,
    )
    outcome = await run_group_turn(
        group=group,
        text="decide the weekend plan",
        user=USER,
        registry=registry,
        conversations=ConversationStore(lead_env["root"]),
    )
    assert outcome["rounds"] == 1
    assert [r["bot_id"] for r in outcome["replies"]] == ["alpha", "beta", "alpha"]
    assert [r["phase"] for r in outcome["replies"]] == ["plan", "report", "synthesis"]
    assert outcome["passed"] == []
    assert outcome["skipped"] == []
    transcript = GroupStore(lead_env["root"]).messages(group.id, include_hidden=True)
    assert [m.get("bot_id") or "user" for m in transcript] == [
        "user",
        "alpha",
        "beta",
        "alpha",
    ]


@pytest.mark.asyncio
async def test_lead_worker_pass_still_synthesizes(lead_env, monkeypatch):
    import server.bot_team.groups as groups_mod

    calls: list[str] = []

    async def scripted(bot, text, context, user, workspace_id, conversations):
        calls.append(bot.bot_id)
        if bot.bot_id == "beta":
            return SimpleNamespace(reply=PASS_SENTINEL, task_id="t", session_id="s")
        if calls.count("alpha") == 1:
            return SimpleNamespace(reply="PLAN: beta does research", task_id="t", session_id="s")
        return SimpleNamespace(reply="SYNTH: adopted", task_id="t", session_id="s")

    monkeypatch.setattr(groups_mod, "run_bot_turn", scripted)
    registry = _register(BotRegistry(), "alpha", "beta")
    group = GroupStore(lead_env["root"]).create(
        name="lead-room",
        bot_ids=["alpha", "beta"],
        admin_bot_id="alpha",
        reply_mode=LEAD_REPLY_MODE,
    )
    outcome = await run_group_turn(
        group=group,
        text="decide the weekend plan",
        user=USER,
        registry=registry,
        conversations=ConversationStore(lead_env["root"]),
    )
    assert [r["phase"] for r in outcome["replies"]] == ["plan", "synthesis"]
    assert outcome["passed"] == ["beta"]
    transcript = GroupStore(lead_env["root"]).messages(group.id, include_hidden=True)
    assert [m.get("bot_id") or "user" for m in transcript] == [
        "user",
        "alpha",
        "beta",
        "alpha",
    ]
    assert transcript[2]["text"] == PASS_SENTINEL
    visible = GroupStore(lead_env["root"]).messages(group.id)
    assert all(m.get("bot_id") != "beta" for m in visible)


@pytest.mark.asyncio
async def test_lead_falls_back_when_admin_gone(lead_env, fake_master):
    registry = _register(BotRegistry(), "beta")
    group = GroupStore(lead_env["root"]).create(
        name="lead-room",
        bot_ids=["alpha", "beta"],
        admin_bot_id="alpha",
        reply_mode=LEAD_REPLY_MODE,
    )
    outcome = await run_group_turn(
        group=group,
        text="decide the weekend plan",
        user=USER,
        registry=registry,
        conversations=ConversationStore(lead_env["root"]),
    )
    assert outcome["skipped"] == ["alpha"]
    assert [r["bot_id"] for r in outcome["replies"]] == ["beta", "beta"]
    assert [r["phase"] for r in outcome["replies"]] == ["plan", "synthesis"]


def test_lead_mode_via_api(client, lead_env):
    _register(BotRegistry(), "alpha", "beta")
    created = client.post(
        "/api/v1/bots/groups",
        json={"name": "lead-room", "bot_ids": ["alpha", "beta"], "reply_mode": "lead"},
    )
    assert created.status_code == 200, created.text
    assert created.json()["group"]["reply_mode"] == "lead"
    group_id = created.json()["group"]["id"]

    bogus = client.patch(f"/api/v1/bots/groups/{group_id}", json={"reply_mode": "bogus"})
    assert bogus.status_code == 422

    back = client.patch(f"/api/v1/bots/groups/{group_id}", json={"reply_mode": "everyone"})
    assert back.status_code == 200
    assert back.json()["group"]["reply_mode"] == "everyone"
