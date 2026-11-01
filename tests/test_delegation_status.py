"""delegation_status: read-only visibility into the running bot's delegations."""

from __future__ import annotations

import json

import pytest

from server.bot_team.delegations import DelegationStore
from server.bot_team.messaging import BotMessageRefused, BotMessenger
from server.bot_team.tool import delegation_status
from server.goal_run.bot_identity import (
    BotRegistry,
    BotSpec,
    bind_current_bot,
    reset_current_bot,
)

USER = {"user_id": "status-user", "username": "status"}


@pytest.fixture
def status_env(tmp_path, monkeypatch):
    monkeypatch.setenv("VEYA_BOTS_ROOT", str(tmp_path / "bots-root"))
    monkeypatch.setenv("VEYA_BOT_REGISTRY_PATH", str(tmp_path / "bot_registry.json"))
    return {"root": tmp_path / "bots-root"}


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


@pytest.mark.asyncio
async def test_status_needs_bound_bot(status_env):
    _register("alpha", "beta")
    with pytest.raises(BotMessageRefused):
        await delegation_status()


@pytest.mark.asyncio
async def test_status_lists_open_and_hides_others(status_env):
    registry = _register("alpha", "beta", "gamma")
    messenger = BotMessenger(registry)
    receipt = await messenger.delegate("alpha", "beta", "do it")
    await messenger.delegate("gamma", "beta", "other bot's work")
    token = bind_current_bot("alpha")
    try:
        listed = json.loads(await delegation_status())
        assert [d["delegation_id"] for d in listed["delegations"]] == [receipt["delegation_id"]]
        assert listed["delegations"][0]["status"] == "queued"
        assert listed["delegations"][0]["reply_mode"] == "resume"
        detail = json.loads(await delegation_status(receipt["delegation_id"]))
        assert detail["delegation"]["target_bot_id"] == "beta"
        unknown = json.loads(await delegation_status("dlg_nope"))
        assert unknown["ok"] is False
    finally:
        reset_current_bot(token)


@pytest.mark.asyncio
async def test_status_done_item_leaves_open_list(status_env):
    registry = _register("alpha", "beta")
    store = DelegationStore()
    item = store.create(from_bot_id="alpha", to_bot_id="beta", text="x", depth=0)
    store.claim(limit=1)
    store.complete(item["id"], reply="done")
    token = bind_current_bot("alpha")
    try:
        listed = json.loads(await delegation_status())
        assert listed["delegations"] == []
        detail = json.loads(await delegation_status(item["id"]))
        assert detail["delegation"]["status"] == "done"
        assert detail["delegation"]["reply"] == "done"
    finally:
        reset_current_bot(token)


def test_register_tools_now_registers_four(status_env):
    from server.bot_team.tool import register_tools
    from server.tool_registry import MasterToolRegistry

    fresh = MasterToolRegistry()
    assert register_tools(fresh) == 4
    assert register_tools(fresh) == 0
    assert sorted(fresh.list_tools()) == [
        "delegate_bot",
        "delegation_status",
        "list_bots",
        "message_bot",
    ]
