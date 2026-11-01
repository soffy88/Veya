"""Auto reply mode for group chats (OpenMausBot method, adapted).

A message naming exactly one member, or whose keywords clearly match one
member's job, is answered by that member alone (phases "direct" / "auto").
Anything ambiguous falls back to the full lead cycle, with the user
message recorded exactly once.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server.bot_team.conversations import PASS_SENTINEL, ConversationStore
from server.bot_team.groups import (
    AUTO_REPLY_MODE,
    GroupStore,
    run_group_turn,
    select_auto_responder,
)
from server.events import EventStore
from server.goal_run.bot_identity import BotRegistry, BotSpec
from server.goal_run.models import GoalRunResponse, GoalStatus
from server.routes import product as product_routes
from server.routes.bot_team import router as bots_router
from server.task_store import TaskStore

USER = {"user_id": "auto-user", "username": "auto"}


@pytest.fixture
def auto_env(tmp_path, monkeypatch):
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


def _register(
    registry: BotRegistry, specs: list[tuple[str, str, str]]
) -> BotRegistry:
    for bot_id, description, instructions in specs:
        registry.register(
            BotSpec(
                bot_id=bot_id,
                owner_id=USER["user_id"],
                name=bot_id.capitalize(),
                description=description,
                instructions=instructions,
            )
        )
    return registry


def _make_group(auto_env, bot_ids, admin):
    return GroupStore(auto_env["root"]).create(
        name="room",
        bot_ids=bot_ids,
        admin_bot_id=admin,
        reply_mode=AUTO_REPLY_MODE,
    )


async def _run(group, auto_env, text):
    return await run_group_turn(
        group=group,
        text=text,
        user=dict(USER),
        registry=BotRegistry(),
        conversations=ConversationStore(auto_env["root"]),
    )


def test_select_auto_responder_keyword(auto_env):
    registry = _register(
        BotRegistry(),
        [
            ("hiker", "Plans mountain hiking trips.", "You plan hiking trips."),
            ("chef", "Cooks dinner menus.", "You cook dinner."),
        ],
    )
    assert (
        select_auto_responder(
            "plan a mountain hike this weekend",
            available=["hiker", "chef"],
            registry=registry,
        )
        == "hiker"
    )
    assert (
        select_auto_responder(
            "what should we do today",
            available=["hiker", "chef"],
            registry=registry,
        )
        is None
    )
    chinese = _register(
        BotRegistry(),
        [
            ("hiker", "负责徒步路线规划。", "你负责规划徒步路线。"),
            ("chef", "负责晚餐菜谱。", "你负责做晚餐。"),
        ],
    )
    assert (
        select_auto_responder(
            "帮我规划一条徒步路线",
            available=["hiker", "chef"],
            registry=chinese,
        )
        == "hiker"
    )


def test_select_auto_responder_tie_is_ambiguous(auto_env):
    registry = _register(
        BotRegistry(),
        [
            ("alpha", "Plans trips.", "You plan trips."),
            ("beta", "Plans trips.", "You plan trips."),
        ],
    )
    assert (
        select_auto_responder(
            "plan a trip", available=["alpha", "beta"], registry=registry
        )
        is None
    )


@pytest.mark.asyncio
async def test_auto_keyword_answers_alone(auto_env, fake_master):
    _register(
        BotRegistry(),
        [
            ("hiker", "Plans mountain hiking trips.", "You plan hiking trips."),
            ("chef", "Cooks dinner menus.", "You cook dinner."),
        ],
    )
    group = _make_group(auto_env, ["hiker", "chef"], "hiker")
    result = await _run(group, auto_env, "plan a mountain hike this weekend")
    assert [r["bot_id"] for r in result["replies"]] == ["hiker"]
    assert result["replies"][0]["phase"] == "auto"
    assert result["replies"][0]["reply"] == "say:hiker"
    transcript = GroupStore(auto_env["root"]).messages(group.id)
    assert [m["role"] for m in transcript] == ["user", "bot"]


@pytest.mark.asyncio
async def test_auto_mention_answers_alone(auto_env, fake_master):
    _register(
        BotRegistry(),
        [
            ("alpha", "Does alpha work.", "You only do alpha work."),
            ("beta", "Does beta work.", "You only do beta work."),
        ],
    )
    group = _make_group(auto_env, ["alpha", "beta"], "alpha")
    result = await _run(group, auto_env, "Beta, what do you think?")
    assert [r["bot_id"] for r in result["replies"]] == ["beta"]
    assert result["replies"][0]["phase"] == "direct"


@pytest.mark.asyncio
async def test_auto_ambiguous_falls_back_to_lead(auto_env, fake_master):
    _register(
        BotRegistry(),
        [
            ("alpha", "Does alpha work.", "You only do alpha work."),
            ("beta", "Does beta work.", "You only do beta work."),
        ],
    )
    group = _make_group(auto_env, ["alpha", "beta"], "alpha")
    result = await _run(group, auto_env, "what should we do today")
    assert [r["phase"] for r in result["replies"]] == ["plan", "report", "synthesis"]
    assert [r["bot_id"] for r in result["replies"]] == ["alpha", "beta", "alpha"]
    transcript = GroupStore(auto_env["root"]).messages(group.id)
    assert [m["role"] for m in transcript].count("user") == 1


@pytest.mark.asyncio
async def test_auto_sole_member_answers_direct(auto_env, fake_master):
    _register(BotRegistry(), [("solo", "Does solo work.", "You only do solo work.")])
    group = _make_group(auto_env, ["solo"], "solo")
    result = await _run(group, auto_env, "hello there")
    assert [r["bot_id"] for r in result["replies"]] == ["solo"]
    assert result["replies"][0]["phase"] == "direct"


def test_auto_mode_via_api(client, auto_env):
    _register(
        BotRegistry(),
        [("alpha", "Does alpha work.", "You only do alpha work.")],
    )
    created = client.post(
        "/api/v1/bots/groups",
        json={"name": "room", "bot_ids": ["alpha"], "reply_mode": "auto"},
    )
    assert created.status_code == 200, created.text
    assert created.json()["group"]["reply_mode"] == "auto"
    bad = client.post(
        "/api/v1/bots/groups",
        json={"name": "room", "bot_ids": ["alpha"], "reply_mode": "telepathy"},
    )
    assert bad.status_code == 422
