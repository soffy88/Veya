"""Async bot-to-bot delegation: queue, dispatcher, team tools, drain hook."""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server.bot_team.conversations import ConversationStore
from server.bot_team.delegations import (
    DelegationStore,
    process_delegations,
)
from server.bot_team.messaging import BotMessageRefused, BotMessenger
from server.bot_team.tool import delegate_bot, list_bots
from server.bot_team.turns import run_bot_turn
from server.events import EventStore
from server.goal_run.bot_identity import (
    BotRegistry,
    BotSpec,
    bind_current_bot,
    reset_current_bot,
)
from server.goal_run.models import GoalRunResponse, GoalStatus
from server.routes import product as product_routes
from server.routes.bot_team import router as bots_router
from server.task_store import TaskStore

USER = {"user_id": "dlg-user", "username": "dlg"}


@pytest.fixture
def dlg_env(tmp_path, monkeypatch):
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
    return {"root": bot_root, "tmp": tmp_path}


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


def _fake_master(monkeypatch, replies: dict[str, str], seen: list[str]):
    async def fake_execute(request: Any) -> GoalRunResponse:
        bot_id = str(request.constraints.metadata.get("bot_id") or "")
        seen.append(bot_id)
        return GoalRunResponse(
            goal_id=f"goal-{bot_id}",
            status=GoalStatus.completed,
            phase="finalized",
            summary=replies.get(bot_id, f"say:{bot_id}"),
        )

    from server.coordinator_master import master_coordinator

    monkeypatch.setattr(master_coordinator, "execute_structured", fake_execute)


def _texts(store: ConversationStore, bot_id: str) -> list[tuple[str, str]]:
    return [(m.get("role", ""), m.get("text", "")) for m in store.list(bot_id)]


def test_store_roundtrip_and_stale_reset(dlg_env):
    store = DelegationStore(root=dlg_env["root"])
    item = store.create(from_bot_id="alpha", to_bot_id="beta", text="hi", depth=0)
    assert item["status"] == "queued"
    assert store.get(item["id"])["to_bot_id"] == "beta"
    assert [i["id"] for i in store.list(status="queued")] == [item["id"]]
    claimed = store.claim(limit=3)
    assert [i["id"] for i in claimed] == [item["id"]]
    assert store.list(status="queued") == []
    # A fresh running item is not stale.
    assert store.reset_stale(timeout_s=600) == 0
    # Backdate it: the next reset re-queues it.
    path = dlg_env["root"] / "delegations.json"
    payload = json.loads(path.read_text())
    payload[0]["updated_ts"] -= 3600
    path.write_text(json.dumps(payload))
    assert store.reset_stale(timeout_s=600) == 1
    assert store.list(status="queued")[0]["id"] == item["id"]
    claimed = store.claim(limit=3)
    done = store.complete(item["id"], reply="ok")
    assert done["status"] == "done" and done["reply"] == "ok"
    failed = store.create(from_bot_id="alpha", to_bot_id="beta", text="x", depth=0)
    store.claim(limit=3)
    assert store.fail(failed["id"], error="boom")["status"] == "failed"


@pytest.mark.asyncio
async def test_delegate_refusals(dlg_env):
    _register("alpha", "beta")
    messenger = BotMessenger(BotRegistry(), conversations=ConversationStore(dlg_env["root"]))
    with pytest.raises(BotMessageRefused):
        await messenger.delegate("alpha", "ghost", "hi")
    with pytest.raises(BotMessageRefused):
        await messenger.delegate("alpha", "alpha", "hi")
    with pytest.raises(BotMessageRefused):
        await messenger.delegate("alpha", "beta", "hi", depth=3)
    receipt = await messenger.delegate("alpha", "beta", "do it", depth=0)
    assert receipt["ok"] is True and receipt["status"] == "queued"
    assert DelegationStore(root=dlg_env["root"]).list()[0]["text"] == "do it"


@pytest.mark.asyncio
async def test_list_bots_tool(dlg_env):
    _register("alpha", "beta")
    payload = json.loads(await list_bots())
    ids = [b["bot_id"] for b in payload["bots"]]
    assert ids == ["alpha", "beta"]
    assert payload["bots"][0]["description"].startswith("alpha does")
    assert payload["bots"][0]["status"]


@pytest.mark.asyncio
async def test_delegate_bot_tool_needs_bound_bot(dlg_env):
    _register("alpha", "beta")
    with pytest.raises(BotMessageRefused):
        await delegate_bot("beta", "hi")
    token = bind_current_bot("alpha")
    try:
        receipt = json.loads(await delegate_bot("beta", "do it"))
    finally:
        reset_current_bot(token)
    assert receipt["status"] == "queued" and receipt["target_bot_id"] == "beta"
    token = bind_current_bot("alpha")
    try:
        with pytest.raises(BotMessageRefused):
            await delegate_bot("alpha", "hi")
    finally:
        reset_current_bot(token)


@pytest.mark.asyncio
async def test_drain_runs_target_turn_and_follow_up(dlg_env, monkeypatch):
    registry = _register("alpha", "beta")
    seen: list[str] = []
    _fake_master(monkeypatch, {"alpha": "say:alpha", "beta": "say:beta"}, seen)
    store = ConversationStore(dlg_env["root"])
    messenger = BotMessenger(registry, conversations=store)
    receipt = await messenger.delegate("alpha", "beta", "do the thing", depth=0)
    outcomes = await process_delegations(conversations=store, user=dict(USER))
    assert len(outcomes) == 1
    assert outcomes[0]["status"] == "done" and outcomes[0]["reply"] == "say:beta"
    assert DelegationStore(root=dlg_env["root"]).list()[0]["status"] == "done"
    # Beta's conversation got the delegated request.
    assert any(t == "do the thing" for _, t in _texts(store, "beta"))
    # Alpha's conversation: the note from message_bot, the follow-up turn
    # input (the reply), and alpha's own follow-up answer.
    alpha_texts = [t for _, t in _texts(store, "alpha")]
    assert "say:beta" in alpha_texts
    assert alpha_texts.count("say:beta") >= 2
    assert "say:alpha" in alpha_texts
    assert seen == ["beta", "alpha"]


@pytest.mark.asyncio
async def test_drain_hook_fires_at_turn_end(dlg_env, monkeypatch):
    registry = _register("alpha", "beta")
    seen: list[str] = []
    _fake_master(monkeypatch, {"alpha": "say:alpha", "beta": "say:beta"}, seen)
    store = ConversationStore(dlg_env["root"])
    messenger = BotMessenger(registry, conversations=store)
    await messenger.delegate("alpha", "beta", "do the thing", depth=0)
    spec = registry.get("alpha")
    assert spec is not None
    result = await run_bot_turn(
        bot=spec, text="hello", user=dict(USER), conversations=store
    )
    assert result.reply == "say:alpha"
    # The turn-end hook drained the queue: beta ran, alpha followed up.
    assert DelegationStore(root=dlg_env["root"]).list()[0]["status"] == "done"
    assert seen == ["alpha", "beta", "alpha"]


@pytest.mark.asyncio
async def test_nested_drain_is_noop(dlg_env, monkeypatch):
    registry = _register("alpha", "beta")
    nested: list[list[dict[str, Any]]] = []
    store = ConversationStore(dlg_env["root"])

    async def fake_execute(request: Any) -> GoalRunResponse:
        bot_id = str(request.constraints.metadata.get("bot_id") or "")
        if bot_id == "beta":
            nested.append(await process_delegations(conversations=store))
        return GoalRunResponse(
            goal_id=f"goal-{bot_id}",
            status=GoalStatus.completed,
            phase="finalized",
            summary=f"say:{bot_id}",
        )

    from server.coordinator_master import master_coordinator

    monkeypatch.setattr(master_coordinator, "execute_structured", fake_execute)
    messenger = BotMessenger(registry, conversations=store)
    await messenger.delegate("alpha", "beta", "do the thing", depth=0)
    outcomes = await process_delegations(conversations=store, user=dict(USER))
    assert len(outcomes) == 1 and outcomes[0]["status"] == "done"
    assert nested == [[]]


@pytest.fixture
def client(dlg_env):
    app = FastAPI()
    from server import auth as _w11_auth

    app.dependency_overrides[_w11_auth.require_user] = lambda: dict(USER)
    app.include_router(bots_router)
    return TestClient(app)


def test_delegations_endpoints(dlg_env, client, monkeypatch):
    _register("alpha", "beta")
    assert client.get("/api/v1/bots/delegations").json() == {"delegations": []}
    assert client.get("/api/v1/bots/delegations?status=bogus").status_code == 422
    store = DelegationStore(root=dlg_env["root"])
    store.create(from_bot_id="alpha", to_bot_id="beta", text="hi", depth=0)
    rows = client.get("/api/v1/bots/delegations?status=queued").json()["delegations"]
    assert len(rows) == 1 and rows[0]["status"] == "queued"
    seen: list[str] = []
    _fake_master(monkeypatch, {"alpha": "say:alpha", "beta": "say:beta"}, seen)
    resp = client.post("/api/v1/bots/delegations/process")
    assert resp.status_code == 200
    processed = resp.json()["processed"]
    assert len(processed) == 1 and processed[0]["status"] == "done"
    assert client.get("/api/v1/bots/delegations?status=done").json()["delegations"]
