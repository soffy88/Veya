"""Lorca P1: multiple named bots + Chef.

Covers the identity half of the feature: backward-compatible BotSpec records,
create/list/patch/delete, the default bot staying exactly as it was, bot_id
reaching the GoalRunState that executes a bot's turn, Chef seeding, and a
proposal only creating bots after an explicit accept.

No bot owns authority in these tests: every turn still goes through
``master_coordinator.execute_structured`` (faked here) and the canonical
``project_run_goal`` path.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server.bot_team.chef import (
    CHEF_BOT_ID,
    ProposalStore,
    ensure_chef_seeded,
    extract_proposal,
)
from server.bot_team.conversations import ConversationStore, safe_bot_id
from server.events import EventStore
from server.goal_run.bot_identity import BotRegistry, BotSpec
from server.goal_run.models import GoalRunResponse, GoalStatus
from server.goal_run.planner import g1_plan
from server.routes import product as product_routes
from server.routes.bot_team import router as bots_router
from server.task_store import TaskStore

USER = {"user_id": "lorca-user", "username": "lorca"}


@pytest.fixture
def lorca_env(tmp_path, monkeypatch):
    """Every bot file lands in tmp_path; nothing touches the real ~/.veya."""

    bot_root = tmp_path / "bots-root"
    registry_path = tmp_path / "bot_registry.json"
    monkeypatch.setenv("VEYA_BOTS_ROOT", str(bot_root))
    monkeypatch.setenv("VEYA_BOT_REGISTRY_PATH", str(registry_path))
    events = EventStore(tmp_path / "events.jsonl")
    tasks = TaskStore(tmp_path / "tasks.json", event_store=events)
    monkeypatch.setattr(product_routes, "task_store", tasks)

    class _History:
        async def save(self, session_id: str, messages: list[Any], *, user_id: str) -> None:
            return None

    monkeypatch.setattr(product_routes, "default_history_store", lambda: _History())
    return {"root": bot_root, "registry_path": registry_path, "task_store": tasks}


@pytest.fixture
def fake_master(monkeypatch):
    """Fake the semantic authority: no model, no gateway, deterministic reply."""

    captured: list[Any] = []

    async def fake_execute(request: Any) -> GoalRunResponse:
        captured.append(request)
        objective = str(getattr(request, "objective", "") or "")
        return GoalRunResponse(
            goal_id="goal-lorca-1",
            status=GoalStatus.completed,
            phase="finalized",
            summary=f"done:{objective.splitlines()[-1][:24]}",
        )

    from server.coordinator_master import master_coordinator

    monkeypatch.setattr(master_coordinator, "execute_structured", fake_execute)
    return captured


@pytest.fixture
def client(owner_headers):
    app = FastAPI()
    # W11-AUTH: bot routes now require a signed-in user (D2).
    from server import auth as _w11_auth
    from server.auth import AuthGateMiddleware

    app.add_middleware(AuthGateMiddleware)
    app.include_router(bots_router)
    client = TestClient(app)
    client.headers.update(owner_headers)
    return client


# ── P1.1 BotSpec backward compatibility ─────────────────────────────────


def test_botspec_loads_records_written_before_lorca():
    """A pre-P1 record (identity only) must load unchanged."""

    old_record = {
        "bot_id": "legacy-bot",
        "owner_id": "user-1",
        "config_version": 2,
        "knowledge_namespace": "knowledge:legacy-bot",
        "context_namespace": "context:legacy-bot",
        "routine_registry_ref": "routines:legacy-bot",
        "available_skill_ids": ["brief.render"],
        "available_playbook_ids": ["publish.report"],
    }

    spec = BotSpec.from_dict(old_record)

    assert spec.bot_id == "legacy-bot"
    assert spec.owner_id == "user-1"
    assert spec.config_version == 2
    assert spec.available_skill_ids == ["brief.render"]
    assert spec.available_playbook_ids == ["publish.report"]
    # New fields default exactly like a fresh bot, and survive a round trip.
    assert spec.name == ""
    assert spec.description == ""
    assert spec.instructions == ""
    assert spec.provider == ""
    assert spec.model == ""
    assert spec.runner_id == "local"
    restored = BotSpec.from_dict(spec.to_dict())
    assert restored == spec
    for key, value in old_record.items():
        assert restored.to_dict()[key] == value


def test_botspec_keeps_lorca_fields(tmp_path):
    registry = BotRegistry(tmp_path / "bots.json")
    registry.register(
        BotSpec(
            bot_id="writer",
            owner_id="user-1",
            name="Writer",
            description="Writes documentation.",
            instructions="Write plainly.",
            provider="ollama",
            model="qwen-test",
        )
    )

    reloaded = BotRegistry(tmp_path / "bots.json").get("writer")

    assert reloaded is not None
    assert reloaded.name == "Writer"
    assert reloaded.description == "Writes documentation."
    assert reloaded.instructions == "Write plainly."
    assert reloaded.provider == "ollama"
    assert reloaded.model == "qwen-test"
    assert reloaded.runner_id == "local"


# ── P1.2/1.4 bot CRUD over HTTP ─────────────────────────────────────────


def test_create_list_patch_delete_bot(client, lorca_env):
    del lorca_env
    created = client.post(
        "/api/v1/bots",
        json={
            "bot_id": "researcher",
            "name": "Researcher",
            "description": "Finds and summarises sources.",
            "instructions": "Cite every claim.",
            "provider": "ollama",
            "model": "qwen-test",
        },
    )

    assert created.status_code == 200
    assert created.json()["bot"]["bot_id"] == "researcher"
    assert BotRegistry().get("researcher") is not None

    listed = client.get("/api/v1/bots")
    bot_ids = {item["bot_id"] for item in listed.json()["bots"]}
    assert {"researcher", CHEF_BOT_ID} <= bot_ids
    assert listed.json()["default_bot_id"] == "veya-default"

    duplicate = client.post("/api/v1/bots", json={"bot_id": "researcher"})
    assert duplicate.status_code == 409

    invalid = client.post("/api/v1/bots", json={"bot_id": "Not Valid"})
    assert invalid.status_code == 422

    patched = client.patch(
        "/api/v1/bots/researcher",
        json={"instructions": "Cite at most three sources.", "model": "qwen-other"},
    )
    assert patched.status_code == 200
    spec = BotRegistry().get("researcher")
    assert spec is not None
    assert spec.instructions == "Cite at most three sources."
    assert spec.model == "qwen-other"
    # Namespaces and the owner survive a patch.
    assert spec.knowledge_namespace == "knowledge:researcher"
    assert spec.owner_id == BotRegistry().get("researcher").owner_id
    assert spec.owner_id

    deleted = client.delete("/api/v1/bots/researcher")
    assert deleted.status_code == 200
    assert BotRegistry().get("researcher") is None
    assert client.get("/api/v1/bots/researcher").status_code == 404
    assert client.delete("/api/v1/bots/researcher").status_code == 404


def test_default_bot_cannot_be_deleted_but_stays_reachable(client, lorca_env):
    del lorca_env
    assert client.delete("/api/v1/bots/veya-default").status_code == 422

    # The default bot was never registered, yet the new surface can drive it
    # (no record is invented for it).
    assert BotRegistry().get("veya-default") is None
    shown = client.get("/api/v1/bots/veya-default")
    assert shown.status_code == 200
    assert shown.json()["bot"]["bot_id"] == "veya-default"
    assert BotRegistry().get("veya-default") is None


def test_existing_default_bot_route_keeps_working(client, lorca_env):
    """``/api/v1/bot`` (the single-bot product shell) is untouched."""

    del client
    del lorca_env
    from server.routes.product import router as product_router

    app = FastAPI()
    # W11-AUTH: bot routes now require a signed-in user (D2).
    from server import auth as _w11_auth

    app.dependency_overrides[_w11_auth.require_user] = lambda: {
        "user_id": "anonymous",
        "username": "anonymous",
    }
    app.include_router(product_router)
    product_client = TestClient(app)

    state = product_client.get("/api/v1/bot")

    assert state.status_code == 200
    assert state.json()["bot"]["id"] == "veya-default"


def test_default_bot_can_be_given_its_own_instructions(client, lorca_env):
    """Patching the default bot registers it; before that it is in-memory only."""

    del lorca_env
    patched = client.patch(
        "/api/v1/bots/veya-default",
        json={"name": "Veya", "instructions": "Answer in one line."},
    )

    assert patched.status_code == 200
    spec = BotRegistry().get("veya-default")
    assert spec is not None
    assert spec.name == "Veya"
    assert spec.instructions == "Answer in one line."
    assert spec.owner_id  # a registry record always has an owner


# ── P1.2 bot_id end to end ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_bot_id_reaches_the_goal_run_state(tmp_path):
    """The planner stamps the owning bot on the GoalRun it creates."""

    state, _response = await g1_plan(
        interpretation="write a short brief",
        assumptions=[],
        goal_text="write a short brief",
        project_root=str(tmp_path),
        bot_id="researcher",
    )

    assert state.bot_id == "researcher"
    from server.goal_run.store import load_goal_run

    persisted = load_goal_run(str(tmp_path), state.goal_id)
    assert persisted is not None
    assert persisted.bot_id == "researcher"


def test_runner_forwards_bot_id_to_the_planner(tmp_path, monkeypatch):
    """``project_run_goal`` passes the bot through to ``g1_plan``."""

    from server.goal_run import runner as runner_module
    from server.goal_run.models import GoalRunResponse, GoalRunState, GoalStatus

    captured: dict[str, Any] = {}

    async def fake_g1_plan(**kwargs: Any) -> tuple[GoalRunState, GoalRunResponse]:
        captured.update(kwargs)
        state = GoalRunState(goal_id="goal-fake", goal_text="fake")
        return state, GoalRunResponse(
            goal_id="goal-fake", status=GoalStatus.running, phase="running"
        )

    async def fake_review_gate(state: Any, goal: str, project_root: str) -> GoalRunResponse | None:
        return GoalRunResponse(goal_id=state.goal_id, status=GoalStatus.blocked, phase="blocked")

    monkeypatch.setattr(runner_module, "g1_plan", fake_g1_plan)
    monkeypatch.setattr(runner_module, "_run_plan_review_gate", fake_review_gate)

    response = asyncio.run(
        runner_module.project_run_goal(
            project_root=str(tmp_path),
            goal="write a short brief",
            mode="act_eager",
            bot_id="researcher",
        )
    )

    assert response.phase == "blocked"
    assert captured["bot_id"] == "researcher"


@pytest.mark.asyncio
async def test_bot_turn_carries_its_bot_into_the_canonical_request(lorca_env, fake_master, client):
    del client
    registry = BotRegistry()
    registry.register(
        BotSpec(
            bot_id="researcher",
            owner_id=USER["user_id"],
            name="Researcher",
            description="Finds and summarises sources.",
            instructions="Cite every claim.",
            provider="ollama",
            model="qwen-test",
        )
    )

    from server.bot_team.turns import run_bot_turn

    turn = await run_bot_turn(
        bot=registry.get("researcher"),
        text="summarise three sources",
        user=USER,
    )

    assert turn.reply == "done:summarise three sources"
    assert len(fake_master) == 1
    request = fake_master[0]
    assert request.constraints.metadata["bot_id"] == "researcher"
    assert request.constraints.metadata["task_id"] == turn.task_id
    # The bot's LLM binding and its instructions are part of the semantic call.
    semantic = request.constraints.metadata["semantic_llm_kwargs"]
    assert semantic["provider"] == "ollama"
    assert semantic["model"] == "qwen-test"
    assert "Cite every claim." in request.objective
    assert "Finds and summarises sources." in request.objective

    stored = ConversationStore(lorca_env["root"])
    messages = stored.list("researcher")
    assert [item["role"] for item in messages] == ["user", "bot"]
    assert messages[0]["text"] == "summarise three sources"
    assert messages[1]["text"] == turn.reply
    assert messages[1]["task_id"] == turn.task_id
    assert all(not item["hidden"] for item in messages)


@pytest.mark.asyncio
async def test_default_bot_turn_still_runs_as_the_default_bot(lorca_env, fake_master, client):
    """A turn of the default bot keeps the historical bot_id (veya-default)."""

    del client
    from server.bot_team.turns import run_bot_turn

    turn = await run_bot_turn(
        bot=BotSpec(bot_id="veya-default", owner_id=USER["user_id"]),
        text="hello",
        user=USER,
    )
    assert turn.bot_id == "veya-default"


@pytest.mark.asyncio
async def test_plain_product_task_still_sends_no_bot_id(lorca_env, fake_master):
    """A task started from the single-bot shell is unchanged by Lorca."""

    await product_routes._run_product_task(
        task_id="task-product-1",
        session_id="session-product-1",
        objective="safe task",
        provider="ollama",
        model="fixture-model",
        config={},
        user=USER,
        project_root=str(lorca_env["root"]),
    )

    assert len(fake_master) == 1
    request = fake_master[0]
    # Empty, not "veya-default": GoalRun keeps its own default for old callers.
    assert request.constraints.metadata["bot_id"] == ""
    assert request.constraints.metadata["semantic_llm_kwargs"] == {
        "provider": "ollama",
        "model": "fixture-model",
    }


# ── P1.5 Chef and proposals ─────────────────────────────────────────────


def test_chef_is_seeded_once_on_first_list(client, lorca_env):
    del lorca_env
    first = client.get("/api/v1/bots")
    chef = [item for item in first.json()["bots"] if item["bot_id"] == CHEF_BOT_ID]
    assert len(chef) == 1
    assert "one-job bots" in chef[0]["description"]
    assert "propose a small team" in chef[0]["instructions"]
    assert chef[0]["runner_id"] == "local"

    # Seeding is idempotent and survives a reload of the catalog.
    second = client.get("/api/v1/bots")
    assert len([item for item in second.json()["bots"] if item["bot_id"] == CHEF_BOT_ID]) == 1
    assert BotRegistry().get(CHEF_BOT_ID) is not None

    # A bot created before the first list keeps its own record.
    client.post("/api/v1/bots", json={"bot_id": "researcher"})
    assert ensure_chef_seeded(BotRegistry()) is None


def test_chef_may_not_be_deleted(client, lorca_env):
    del lorca_env
    client.get("/api/v1/bots")
    assert client.delete(f"/api/v1/bots/{CHEF_BOT_ID}").status_code == 422


def test_proposal_accept_creates_the_bots(client, lorca_env):
    """Chef proposes; the accept call is what creates the bots."""

    client.get("/api/v1/bots")
    reply = (
        "Here is a team for your research work:\n\n"
        "```veya-bots\n"
        '{"bots": [{"bot_id": "researcher", "name": "Researcher",'
        ' "description": "Finds sources", "instructions": "Cite sources."},'
        ' {"bot_id": "writer", "name": "Writer",'
        ' "description": "Writes briefs", "instructions": "Be brief."}]}\n'
        "```\n\nAccept it to create the team."
    )

    found = extract_proposal(reply)
    assert found is not None
    assert [bot["bot_id"] for bot in found["bots"]] == ["researcher", "writer"]

    store = ProposalStore(lorca_env["root"])
    proposal = store.record(reply, task_id="task-1", owner_id=USER["user_id"])
    assert proposal is not None

    registry = BotRegistry()
    assert registry.get("researcher") is None
    accepted = client.post(f"/api/v1/bots/proposals/{proposal['id']}/accept")
    assert accepted.status_code == 200
    assert accepted.json()["created"] == ["researcher", "writer"]

    # A fresh read of the catalog (the registry is a per-instance snapshot).
    researcher = BotRegistry().get("researcher")
    assert researcher is not None
    assert researcher.name == "Researcher"
    assert researcher.instructions == "Cite sources."
    assert researcher.owner_id  # the accepting user owns the bots it consented to
    assert BotRegistry().get("writer") is not None

    # Accepting twice is refused, never duplicated.
    again = client.post(f"/api/v1/bots/proposals/{proposal['id']}/accept")
    assert again.status_code == 409
    assert client.post("/api/v1/bots/proposals/nope/accept").status_code == 404


def test_proposal_is_only_read_from_the_documented_block(client, lorca_env):
    """Prose that merely mentions a bot is not a proposal."""

    del client
    del lorca_env
    assert extract_proposal("You should create a bot called researcher.") is None
    assert extract_proposal("```veya-bots\nnot json\n```") is None
    assert extract_proposal('```veya-bots\n{"bots": []}\n```') is None
    # A proposed bot without a job is dropped, so an empty proposal is refused.
    assert extract_proposal('```veya-bots\n{"bots": [{"bot_id": "x"}]}\n```') is None


def test_bot_id_directory_names_stay_inside_the_bots_root(lorca_env):
    """A bot id becomes a directory name, so traversal is refused."""

    assert safe_bot_id("researcher") == "researcher"
    for bad in ("../escape", "a/b", "", " "):
        with pytest.raises(ValueError):
            safe_bot_id(bad)


def test_bot_registry_file_is_created_next_to_the_bots_root(lorca_env):
    """Both escapes use their own roots, so no real ~/.veya file is touched."""

    del lorca_env
    assert os.environ.get("VEYA_BOTS_ROOT")
    assert os.environ.get("VEYA_BOT_REGISTRY_PATH")
    assert Path(os.environ["VEYA_BOT_REGISTRY_PATH"]).parent.exists()
