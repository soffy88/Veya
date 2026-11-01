"""Room-member hold directives: @bot stop / resume, stash and redelivery."""

from __future__ import annotations

from typing import Any

import pytest

from server.bot_team.conversations import PASS_SENTINEL, ConversationStore
from server.bot_team.groups import GroupStore, run_group_turn
from server.bot_team.hold_directives import classify_hold_directive
from server.events import EventStore
from server.goal_run.bot_identity import BotRegistry, BotSpec
from server.goal_run.models import GoalRunResponse, GoalStatus
from server.routes import product as product_routes
from server.task_store import TaskStore

USER = {"user_id": "hold-user", "username": "hold"}


@pytest.fixture
def hold_env(tmp_path, monkeypatch):
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


def _register(*bot_ids: str) -> BotRegistry:
    registry = BotRegistry()
    for bot_id in bot_ids:
        registry.register(
            BotSpec(
                bot_id=bot_id,
                owner_id=USER["user_id"],
                name=bot_id.capitalize(),
                description=f"{bot_id} does {bot_id} work.",
                instructions=f"You are {bot_id}.",
            )
        )
    return registry


def _fake_master(monkeypatch, replies: dict[str, str], seen: list[str], objectives: dict[str, list[str]] | None = None):
    async def fake_execute(request: Any) -> GoalRunResponse:
        bot_id = str(request.constraints.metadata.get("bot_id") or "")
        seen.append(bot_id)
        if objectives is not None:
            objectives.setdefault(bot_id, []).append(str(getattr(request, "objective", "") or ""))
        return GoalRunResponse(
            goal_id=f"goal-{bot_id}",
            status=GoalStatus.completed,
            phase="finalized",
            summary=replies.get(bot_id, f"say:{bot_id}"),
        )

    from server.coordinator_master import master_coordinator

    monkeypatch.setattr(master_coordinator, "execute_structured", fake_execute)


def _group(hold_env, registry, bot_ids, reply_mode="everyone", admin=""):
    store = GroupStore(hold_env["root"])
    return store.create(
        name="hold-room",
        bot_ids=list(bot_ids),
        admin_bot_id=admin or bot_ids[0],
        reply_mode=reply_mode,
    )


async def _run(hold_env, registry, group, text):
    return await run_group_turn(
        group=group,
        text=text,
        user=dict(USER),
        registry=registry,
        conversations=ConversationStore(hold_env["root"]),
    )


def test_classify_hold_directive():
    registry = _register("alpha", "beta")
    assert classify_hold_directive("@beta stop", ["alpha", "beta"], registry).holds == ("beta",)
    assert classify_hold_directive("@beta 停一下", ["alpha", "beta"], registry).holds == ("beta",)
    assert classify_hold_directive("@beta resume", ["alpha", "beta"], registry).resumes == ("beta",)
    assert classify_hold_directive("继续", ["alpha", "beta"], registry).resume_all is True
    assert classify_hold_directive("@beta stop @alpha resume", ["alpha", "beta"], registry).holds == ("beta",)
    assert classify_hold_directive("@beta", ["alpha", "beta"], registry) is None
    assert classify_hold_directive("stop", ["alpha", "beta"], registry) is None
    assert classify_hold_directive("@beta stop, 顺便说说昨天", ["alpha", "beta"], registry) is None
    assert classify_hold_directive("> @beta stop", ["alpha", "beta"], registry) is None
    assert classify_hold_directive("`@beta stop`", ["alpha", "beta"], registry) is None
    assert classify_hold_directive("@beta 今天天气不错，我们 stop 一下吧", ["alpha", "beta"], registry) is None


@pytest.mark.asyncio
async def test_hold_resume_and_stash_redelivery(hold_env, monkeypatch):
    registry = _register("alpha", "beta")
    seen: list[str] = []
    objectives: dict[str, list[str]] = {}
    _fake_master(monkeypatch, {"alpha": "say:alpha", "beta": "say:beta"}, seen, objectives)
    group = _group(hold_env, registry, ["alpha", "beta"])

    result = await _run(hold_env, registry, group, "@beta stop")
    assert result["replies"] == [] and result["held"] == ["beta"]
    assert seen == []

    result = await _run(hold_env, registry, group, "hello while held")
    assert [r["bot_id"] for r in result["replies"]] == ["alpha"]
    assert "beta" not in seen
    store = GroupStore(hold_env["root"])
    assert store.held_context(group.id, "beta") == ["hello while held"]

    result = await _run(hold_env, registry, group, "@beta resume")
    assert result["resumed"] == ["beta"] and result["replies"] == []

    result = await _run(hold_env, registry, group, "hello again")
    assert [r["bot_id"] for r in result["replies"]] == ["alpha", "beta"]
    assert any("hello while held" in obj for obj in objectives.get("beta", []))
    assert store.held_context(group.id, "beta") == []


@pytest.mark.asyncio
async def test_hold_lead_mode_filters_held_lead(hold_env, monkeypatch):
    registry = _register("alpha", "beta")
    seen: list[str] = []
    _fake_master(monkeypatch, {"alpha": "say:alpha", "beta": "say:beta"}, seen)
    group = _group(hold_env, registry, ["alpha", "beta"], reply_mode="lead", admin="alpha")

    await _run(hold_env, registry, group, "@alpha stop")
    result = await _run(hold_env, registry, group, "hello")
    assert result["replies"]
    assert {r["bot_id"] for r in result["replies"]} == {"beta"}


@pytest.mark.asyncio
async def test_empty_reply_counts_as_pass(hold_env, monkeypatch):
    import server.bot_team.turns as turns_mod

    registry = _register("alpha", "beta")
    seen: list[str] = []
    _fake_master(monkeypatch, {"alpha": "say:alpha", "beta": "say:beta"}, seen)
    real_extract = turns_mod.extract_reply
    monkeypatch.setattr(
        turns_mod, "extract_reply", lambda response, *, bot: ""
    )
    try:
        group = _group(hold_env, registry, ["alpha", "beta"])
        result = await _run(hold_env, registry, group, "hello")
    finally:
        monkeypatch.setattr(turns_mod, "extract_reply", real_extract)
    assert result["replies"] == []
    assert sorted(result["passed"]) == ["alpha", "beta"]
    store = GroupStore(hold_env["root"])
    visible = [m for m in store.messages(group.id) if m.get("role") == "bot"]
    assert all((m.get("text") or "").strip() for m in visible)
    hidden = [m for m in store.messages(group.id, include_hidden=True) if m.get("role") == "bot"]
    assert all(m.get("text") == PASS_SENTINEL for m in hidden)


def test_stash_cap(hold_env):
    registry = _register("alpha", "beta")
    store = GroupStore(hold_env["root"])
    group = store.create(name="cap", bot_ids=["alpha", "beta"], admin_bot_id="alpha")
    store.update(group.id, held_bot_ids=["beta"])
    for i in range(25):
        store.stash_held_message(group.id, "beta", f"msg {i}")
    assert store.held_context(group.id, "beta") == [f"msg {i}" for i in range(5, 25)]
