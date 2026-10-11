"""tests/test_bot_landing_e2e_more.py — W11 Group A2 end-to-end tests.

Covers scenarios S5..S9 under the full production always-on flag set:
- S5: Memory (remember fact, retrieve in new conversation, forget)
- S6: Routine / Always-On (proactive tick posts to conversation without user turn, pause, delete)
- S7: Loop Goal & Checkpoint Cards (2 rounds, per-round card, finish card, verdict route accepts)
- S8: Group Roles & @mention (mention routing, admin-only permission matrix)
- S9: Secrets Never Leak (request flow, refused in group, masked in replies and conversation history)

Run with: .venv/bin/python -m pytest -q tests/test_bot_landing_e2e_more.py
"""

from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server import auth as auth_mod
from server.app import app as root_app
from server.bot_team.conversations import ConversationStore
from server.bot_team.groups import GroupStore
from server.bot_team.memory_layers import LayeredMemoryStore
from server.bot_team.routine_clock import RoutineClock
from server.bot_team.secrets import BotSecretStore
from server.goal_run.bot_identity import BotRegistry, BotSpec
from server.goal_run.models import RoutineSpec
from server.goal_run.routine_registry import RoutineRegistry
from server.nano_w10.loop_mode import (
    CARD_KIND,
    FinishCheckpointGate,
    LoopRunPolicy,
    card_key,
    reset_finish_checkpoint,
)
from veya.oskill.pure.group_roles import (
    ACTION_DELETE_GROUP,
    ACTION_EDIT_NOTICE,
    ACTION_EDIT_REPLY_MODE,
    ACTION_MENTION,
    ACTION_SPEAK,
    evaluate_group_permission,
    select_replying_bots,
)
from veya.oskill.pure.loop_policy import MODE_LOOP, checkpoint_card, parse_loop_brief

PROD_FLAGS: dict[str, str] = {
    "VEYA_TURN_QUEUE": "1",
    "VEYA_WAKE_ROUTER": "1",
    "VEYA_LOOP_MODE": "1",
    "VEYA_FINISH_CHECKPOINT": "1",
    "VEYA_LIVE_STREAM": "1",
    "VEYA_PROMPT_CACHE": "1",
    "VEYA_CONTEXT_STEWARD": "1",
    "VEYA_MEMORY_REVIEW": "1",
    "VEYA_READ_OWN_CHAT": "1",
    "VEYA_BOT_MEMORY_MD": "1",
    "VEYA_PERSISTENT_WORKERS": "1",
    "VEYA_AUTO_REVIEW": "1",
    "VEYA_GROUP_ROLES": "1",
    "VEYA_BOT_SECRETS": "1",
    "VEYA_A11Y_FIRST": "1",
    "VEYA_DOM_READER": "1",
    "VEYA_CHANNEL_LADDER": "1",
    "VEYA_TOOL_ERROR_FRIENDLY": "1",
    "VEYA_SEARCH_FALLBACK": "1",
    "VEYA_CHANNEL_STATUS": "1",
    "VEYA_APPROVAL_GATES": "1",
}


@pytest.fixture(autouse=True)
def apply_prod_flags_and_tmp_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Ensure all always-on flags are ON and filesystem stores are isolated."""
    for flag, val in PROD_FLAGS.items():
        monkeypatch.setenv(flag, val)

    bots_root = tmp_path / "bots"
    bots_root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("VEYA_BOTS_ROOT", str(bots_root))
    monkeypatch.setenv("VEYA_VAULT_DIR", str(tmp_path / "vault"))
    monkeypatch.setenv("VEYA_ROUTINE_CLOCK_PATH", str(tmp_path / "routine_clock.json"))
    monkeypatch.setenv("VEYA_ROUTINE_REGISTRY_PATH", str(tmp_path / "routines.json"))

    from server.bot_team import secrets as secrets_mod

    custom_secrets = BotSecretStore(root=bots_root, vault_dir=tmp_path / "vault")
    monkeypatch.setattr(secrets_mod, "bot_secret_store", lambda: custom_secrets)

    reset_finish_checkpoint()
    yield
    reset_finish_checkpoint()


@pytest.fixture
def test_user():
    username = f"u_a2_{uuid.uuid4().hex[:8]}"
    user = auth_mod.create_user(username, "pass_test_123")
    token = auth_mod.issue_token(user["user_id"])
    return {
        "user_id": user["user_id"],
        "token": token,
        "headers": {"Authorization": f"Bearer {token}"},
    }


@pytest.fixture
def client(test_user):
    c = TestClient(root_app)
    c.headers.update(test_user["headers"])
    return c


# ---------------------------------------------------------------------------
# S5: Memory
# ---------------------------------------------------------------------------


def test_s5_memory(test_user, client):
    """S5: remember fact -> new conversation recalls fact -> forget works."""
    from runtime import bot_scope
    from server.bot_team.memory import inject_memory_context
    from server.bot_team.memory_layers import forget, recall, remember

    bot_id = "bot_coffee_expert"
    bot_reg = BotRegistry()
    bot_reg.register(
        BotSpec(bot_id=bot_id, owner_id=test_user["user_id"], name="CoffeeExpert", runner_id="local")
    )

    token = bot_scope.bind_current_bot(bot_id)
    try:
        # 1. Bot learns preference
        res = json.loads(remember("我喜欢黑咖啡"))
        assert res.get("ok") is True

        store = LayeredMemoryStore(bot_id)
        assert "我喜欢黑咖啡" in store.core_text("bot")

        # 2. Recall in session
        state = json.loads(recall())
        assert any("我喜欢黑咖啡" in entry for entry in state.get("entries", []))

        # 3. New conversation context injection
        class FakeSemanticAgent:
            def __init__(self):
                self._histories = {}

            def get_system_prompt(self):
                return "You are a helpful assistant."

        agent = FakeSemanticAgent()
        new_session_id = f"sess_{uuid.uuid4().hex[:8]}"
        injected = inject_memory_context(agent, new_session_id, bot_id)
        assert injected is True

        system_messages = agent._histories[new_session_id]
        combined_prompt = " ".join(str(m.get("content")) for m in system_messages)
        assert "我喜欢黑咖啡" in combined_prompt

        # 4. Forget preference
        forget_res = json.loads(forget("黑咖啡"))
        assert forget_res.get("ok") is True
        assert "我喜欢黑咖啡" not in store.core_text("bot")
    finally:
        bot_scope.reset_current_bot(token)


# ---------------------------------------------------------------------------
# S6: Routine / Always-On
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_s6_routine_always_on(test_user, client, tmp_path):
    """S6: routine fires on schedule, posts to conversation, pause and delete work."""
    bot_id = "bot_clock_pro"
    b_reg = BotRegistry()
    b_reg.register(
        BotSpec(bot_id=bot_id, owner_id=test_user["user_id"], name="ClockPro", runner_id="local")
    )

    # 1. Create routine via API
    res_create = client.post(
        f"/api/v1/bots/{bot_id}/routines",
        json={
            "objective": "报一次时间",
            "schedule": "every 2m",
            "enabled": True,
        },
    )
    assert res_create.status_code == 200
    r_data = res_create.json()
    routine_id = r_data["routine_id"]
    assert r_data["enabled"] is True

    # 2. Clock tick runs and posts to conversation
    routines_reg = RoutineRegistry(bot_id=bot_id)
    clock_file = tmp_path / "test_clock.json"
    clock = RoutineClock(routines_reg, clock_path=str(clock_file))

    # First tick initializes last_fired to current time
    await clock.tick()

    # Fast forward last_fired past 2 minutes so due_iso triggers
    past_iso = "2020-01-01T00:00:00+00:00"
    clock.last_fired[routine_id] = past_iso
    clock._save_state()

    # Tick fires routine
    from unittest.mock import AsyncMock, patch

    with patch(
        "server.coordinator_master.master_coordinator.execute_structured",
        AsyncMock(return_value="现在时间是 11:15"),
    ):
        handoffs = await clock.tick()
    assert len(handoffs) == 1
    assert handoffs[0]["routine_id"] == routine_id

    # Check conversation received bot message autonomously without user turn
    res_msgs = client.get(f"/api/v1/bots/{bot_id}/messages")
    assert res_msgs.status_code == 200
    msgs = res_msgs.json().get("messages", [])
    routine_msgs = [m for m in msgs if (m.get("meta") or {}).get("source") == "routine"]
    assert len(routine_msgs) >= 1
    assert routine_msgs[0]["role"] == "bot"

    # 3. Pause routine
    res_pause = client.patch(
        f"/api/v1/bots/{bot_id}/routines/{routine_id}",
        json={"enabled": False},
    )
    assert res_pause.status_code == 200
    assert res_pause.json()["enabled"] is False

    # Further tick does not fire
    clock.last_fired[routine_id] = past_iso
    handoffs_paused = await clock.tick()
    assert len(handoffs_paused) == 0

    # 4. Delete routine
    res_del = client.delete(f"/api/v1/bots/{bot_id}/routines/{routine_id}")
    assert res_del.status_code == 200

    res_check = client.patch(
        f"/api/v1/bots/{bot_id}/routines/{routine_id}",
        json={"enabled": True},
    )
    assert res_check.status_code == 404


# ---------------------------------------------------------------------------
# S7: Loop Goal & Checkpoint Cards
# ---------------------------------------------------------------------------


def test_s7_loop_goal_and_checkpoint_cards(test_user, client):
    """S7: 2 rounds of loop goal generate round & finish cards; verdict route accepts."""
    bot_id = "bot_loop_worker"
    b_reg = BotRegistry()
    b_reg.register(
        BotSpec(bot_id=bot_id, owner_id=test_user["user_id"], name="LoopWorker", runner_id="local")
    )

    goal_run_id = f"gr_{uuid.uuid4().hex[:8]}"

    # 1. Loop brief parsing
    brief = parse_loop_brief("loop 2 rounds on verification")
    assert brief["mode"] == MODE_LOOP
    assert brief["turn_budget"] == 2

    # 2. Gate & Policy posting
    posted_cards: list[dict[str, Any]] = []

    def mock_post(c_bot_id, text, meta):
        ConversationStore().append(
            c_bot_id,
            role="bot",
            text=text,
            meta=meta,
        )
        posted_cards.append(meta)

    gate = FinishCheckpointGate(post_to_chat=mock_post)
    policy = LoopRunPolicy(
        post_card=lambda r_id, card: gate.publish(bot_id, r_id, card, flag="VEYA_LOOP_MODE")
    )

    # Round 1
    d1 = policy.decide(
        goal_run_id,
        mode=MODE_LOOP,
        round_index=1,
        rounds_done=1,
        turn_budget=2,
        summary="round 1 complete",
    )
    assert d1["stop"] is False
    assert d1["next"] in ("checkpoint", "continue")

    # Round 2 (reaches budget)
    d2 = policy.decide(
        goal_run_id,
        mode=MODE_LOOP,
        round_index=2,
        rounds_done=2,
        turn_budget=2,
        summary="round 2 complete",
    )
    assert d2["stop"] is True

    # Finish card (one per run)
    finish_res = gate.post(
        goal_run_id,
        bot_id=bot_id,
        summary="all 2 rounds finished successfully",
    )
    assert finish_res["posted"] is True
    finish_card_id = finish_res["card_id"]

    # Verify cards in conversation
    msgs = ConversationStore().list(bot_id, include_hidden=True)
    card_ids = [m.get("meta", {}).get("card_id") for m in msgs if m.get("meta")]
    assert finish_card_id in card_ids
    assert card_key(bot_id, goal_run_id, mode=MODE_LOOP, round_index=1) in card_ids

    # 3. User verdict accepts finish card
    from server.nano_w10 import loop_mode as lm_mod

    # Wire gate singleton to use the test gate
    lm_mod._GATE = gate

    res_verdict = client.post(
        f"/api/v1/bots/{bot_id}/checkpoints/{finish_card_id}",
        json={"verdict": "accept"},
    )
    assert res_verdict.status_code == 200
    v_data = res_verdict.json()
    assert v_data["status"] == "recorded"
    assert v_data["verdict"] == "accept"
    assert v_data["card_id"] == finish_card_id


# ---------------------------------------------------------------------------
# S8: Group Roles & @mention
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_s8_group_roles_and_mention(test_user, client, monkeypatch):
    """S8: @mention routes to targeted bot, admin-only actions refused for non-admin."""
    b_admin = "bot_lead"
    b_worker = "bot_coder"
    b_reg = BotRegistry()
    b_reg.register(
        BotSpec(bot_id=b_admin, owner_id=test_user["user_id"], name="Lead", runner_id="local")
    )
    b_reg.register(
        BotSpec(bot_id=b_worker, owner_id=test_user["user_id"], name="Coder", runner_id="local")
    )

    g_store = GroupStore()
    group = g_store.create(
        name="Engineering",
        bot_ids=[b_admin, b_worker],
        admin_bot_id=b_admin,
        reply_mode="mention-only",
    )

    # 1. Test pure mention routing
    # Targeting @bot_coder only
    targets = select_replying_bots(
        reply_mode="mention-only",
        admin_bot_id=b_admin,
        member_bot_ids=[b_admin, b_worker],
        text=f"Please write tests @{b_worker}",
    )
    assert targets == [b_worker]

    # No mention in mention-only defaults to admin
    default_targets = select_replying_bots(
        reply_mode="mention-only",
        admin_bot_id=b_admin,
        member_bot_ids=[b_admin, b_worker],
        text="General broadcast message",
    )
    assert default_targets == [b_admin]

    # 2. Run group turn with mock turn execution
    from server.bot_team.groups import run_group_turn

    turns_run: list[str] = []

    async def fake_turn(*, bot, **kwargs):
        turns_run.append(bot.bot_id)
        return SimpleNamespace(reply=f"{bot.bot_id} done", task_id="t1", session_id="s1")

    monkeypatch.setattr("server.bot_team.groups.run_bot_turn", fake_turn)

    await run_group_turn(
        group=group,
        text=f"Check this @{b_worker}",
        user=test_user,
        registry=b_reg,
    )
    assert turns_run == [b_worker]

    # 3. Admin-only action refused for non-admin
    # Admin has permission to edit notice / edit reply mode / delete group
    assert evaluate_group_permission("admin", ACTION_EDIT_NOTICE, is_own_group=True) is True
    assert evaluate_group_permission("admin", ACTION_EDIT_REPLY_MODE, is_own_group=True) is True

    # Member is refused for administrative actions
    assert evaluate_group_permission("member", ACTION_EDIT_NOTICE, is_own_group=True) is False
    assert evaluate_group_permission("member", ACTION_EDIT_REPLY_MODE, is_own_group=True) is False
    assert evaluate_group_permission("member", ACTION_DELETE_GROUP, is_own_group=True) is False

    # Member can only speak and mention
    assert evaluate_group_permission("member", ACTION_SPEAK, is_own_group=True) is True
    assert evaluate_group_permission("member", ACTION_MENTION, is_own_group=True) is True


# ---------------------------------------------------------------------------
# S9: Secrets Never Leak
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_s9_secrets_never_leak(test_user, client):
    """S9: request_secret flow, refused in group, secret value never leaks into replies/logs."""
    bot_id = "bot_secure"
    b_reg = BotRegistry()
    b_reg.register(
        BotSpec(bot_id=bot_id, owner_id=test_user["user_id"], name="SecureBot", runner_id="local")
    )

    from server.bot_team.secrets import bot_secret_store

    sec_store = bot_secret_store()

    # 1. request_secret refused in group
    with pytest.raises(ValueError, match="SECRETS_NOT_IN_GROUP"):
        sec_store.request_secret("PROD_KEY", "prod database access", bot_id, is_group=True)

    # 2. request_secret succeeds in direct chat
    card = sec_store.request_secret("PROD_KEY", "prod database access", bot_id, is_group=False)
    assert card["type"] == "secret_request_card"
    assert card["name"] == "PROD_KEY"

    # 3. User sets secret value
    secret_value = "super_secret_token_value_9876543210"
    sec_store.set_secret_value("PROD_KEY", secret_value, scope="this bot", requesting_bot_id=bot_id)

    # 4. list_secrets returns metadata only, never secret value
    records = sec_store.list_secrets(requesting_bot_id=bot_id)
    assert len(records) >= 1
    serialized = json.dumps(records)
    assert secret_value not in serialized
    assert "PROD_KEY" in serialized

    # 5. Masking in ConversationStore (message history)
    c_store = ConversationStore()
    msg = c_store.append(
        bot_id,
        role="user",
        text=f"Here is the token: {secret_value}",
    )
    assert secret_value not in msg["text"]
    assert "[secret:PROD_KEY]" in msg["text"]

    loaded_msgs = c_store.list(bot_id)
    raw_history = json.dumps(loaded_msgs)
    assert secret_value not in raw_history
    assert "[secret:PROD_KEY]" in raw_history

    # 6. Masking in GroupStore messages
    g_store = GroupStore()
    g_msg = g_store.append_message(
        "grp_sec",
        role="user",
        text=f"leaking in group: {secret_value}",
    )
    assert secret_value not in g_msg["text"]
    assert "[secret:PROD_KEY]" in g_msg["text"]

    # 7. Masking in finish_bot_turn reply
    from server.bot_team.turns import BotTurnStart, finish_bot_turn

    start = BotTurnStart(bot_id=bot_id, task_id="t_sec", session_id="s_sec", trace_id="tr_sec")

    # Mock product task returning the secret value in summary
    class FakeProductResponse:
        summary = f"I connected with {secret_value}"
        status = "completed"

    from server.routes import product as product_routes

    async def fake_run(*args, **kwargs):
        return FakeProductResponse()

    import unittest.mock as mock

    with mock.patch.object(product_routes, "_run_product_task", fake_run):
        turn_result = await finish_bot_turn(
            bot=b_reg.get(bot_id),
            start=start,
            text="check connection",
            user=test_user,
        )

    # BotTurnResult reply must NOT contain secret value
    assert secret_value not in turn_result.reply
    assert "[secret:PROD_KEY]" in turn_result.reply

    # The persisted bot reply in conversation must NOT contain secret value
    bot_msgs = [m for m in c_store.list(bot_id) if m["role"] == "bot"]
    assert any("[secret:PROD_KEY]" in m["text"] for m in bot_msgs)
    assert not any(secret_value in m["text"] for m in bot_msgs)
