"""W11-E: a mid-turn correction reaches the turn (S4); forget really forgets (S5)."""

from __future__ import annotations

from typing import Any

import pytest


# ── S5 ──────────────────────────────────────────────────────────────────────


def test_forget_removes_fact_from_core_scratch_and_daily(tmp_path):
    from server.bot_team.memory_layers import LayeredMemoryStore

    store = LayeredMemoryStore("bot_s5", bots_root=tmp_path)
    store.append_entry("用户喜欢黑咖啡", source="chat")
    store.append_daily("用户说：我喜欢黑咖啡")
    assert store.search("黑咖啡", limit=10)

    result = store.remove_entry("我喜欢黑咖啡")
    assert result["ok"], result
    assert not store.search("黑咖啡", limit=10)

    # The forget request itself is logged afterwards; it must not bring it back.
    store.append_daily("用户：请忘掉我喜欢黑咖啡这件事")
    assert not store.search("喜欢黑咖啡", limit=10)
    journal_ops = {row.get("op") for row in store.journal(limit=20)}
    assert "remove" in journal_ops


def test_forget_unknown_fact_reports_not_found(tmp_path):
    from server.bot_team.memory_layers import LayeredMemoryStore

    store = LayeredMemoryStore("bot_s5b", bots_root=tmp_path)
    store.append_entry("用户住在杭州", source="chat")
    result = store.remove_entry("喜欢绿茶")
    assert result["ok"] is False
    assert store.search("杭州", limit=10)


# ── S4 ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_correction_during_turn_reaches_the_reply(monkeypatch, tmp_path):
    from server.bot_team import turns as turns_mod
    from server.bot_team.conversations import ConversationStore
    from server.goal_run.bot_identity import BotSpec
    from server.nano_w10 import turn_queue as tq
    from server.routes import product as product_routes

    monkeypatch.setenv("VEYA_TURN_QUEUE", "1")
    monkeypatch.setenv("VEYA_BOTS_ROOT", str(tmp_path / "bots"))
    queue = tq.get_turn_queue()
    bot = BotSpec(bot_id="bot_s4_unit", owner_id="u1", name="S4", runner_id="local")
    objectives: list[str] = []
    states: list[str] = []

    async def fake_open(**kwargs: Any) -> tuple[str, str]:
        return "s_s4", "t_s4"

    async def fake_run(**kwargs: Any) -> dict[str, Any]:
        objectives.append(kwargs["objective"])
        states.append(queue.turn_state(bot.bot_id))
        if len(objectives) == 1:
            # The user corrects while the model is still answering.
            queue.steer(bot.bot_id, "改成用英文回答")
            return {"status": "completed", "reply": "长城有两千多年的历史。"}
        return {"status": "completed", "reply": "The Great Wall is over two thousand years old."}

    monkeypatch.setattr(product_routes, "_open_task_projection", fake_open)
    monkeypatch.setattr(product_routes, "_run_product_task", fake_run)
    conversations = ConversationStore(tmp_path / "conv") if _takes_root(ConversationStore) else None

    result = await turns_mod.run_bot_turn(
        bot=bot, text="介绍长城", user={"user_id": "u1"}, conversations=conversations
    )

    assert states[0] == "running", "a turn must mark its conversation running"
    assert len(objectives) == 2
    assert "改成用英文回答" in objectives[1]
    assert result.reply.startswith("The Great Wall")
    assert queue.turn_state(bot.bot_id) != "running", "the turn must release the conversation"


def _takes_root(cls: Any) -> bool:
    import inspect

    params = list(inspect.signature(cls.__init__).parameters)
    return len(params) > 1
