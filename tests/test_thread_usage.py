"""Per-thread usage ledger: turns, tools, and honest nulls for tokens/cost."""

from __future__ import annotations

import pytest

from server.bot_team.conversations import ConversationStore
from server.bot_team.thread_usage import (
    TurnUsage,
    bind_turn_usage,
    current_turn_usage,
    note_tool_call,
    record_turn,
    reset_thread_usage,
    reset_turn_usage,
    thread_usage_snapshot,
)
from server.bot_team.turns import BotTurnStart, finish_bot_turn
from server.goal_run.bot_identity import BotSpec
from server.tool_registry import MasterToolRegistry


@pytest.fixture(autouse=True)
def _clean_ledger():
    reset_thread_usage()
    yield
    reset_thread_usage()


def test_accumulator_counts_tool_calls():
    usage = TurnUsage()
    usage.note_tool("hello", 12.5)
    usage.note_tool(42, 3.0)
    assert usage.tool_calls == 2
    assert usage.tool_result_chars == 5
    assert usage.tool_duration_ms == pytest.approx(15.5)


def test_ledger_merges_turns_and_keeps_token_fields_null():
    row = record_turn(
        "grp_1", bot_id="beta", status="completed",
        usage=TurnUsage(tool_calls=2, tool_result_chars=40, tool_duration_ms=9.0),
    )
    assert row["turns"] == 1
    assert row["input_tokens"] is None
    assert row["cost_usd"] is None
    row = record_turn(
        "grp_1", bot_id="alpha", status="stalled",
        usage=TurnUsage(tool_calls=1, tool_result_chars=3, tool_duration_ms=1.0),
    )
    assert row["turns"] == 2
    assert row["tool_calls"] == 3
    assert row["last_speaker"] == "alpha"
    assert row["last_status"] == "stalled"
    assert thread_usage_snapshot()["grp_1"]["turns"] == 2


def test_metering_seam_fills_token_fields_when_a_source_exists():
    row = record_turn(
        "grp_1", bot_id="beta", status="completed",
        metering={"input_tokens": 100, "output_tokens": 20, "cost_usd": 0.01},
    )
    assert row["input_tokens"] == 100
    assert row["output_tokens"] == 20
    assert row["cached_tokens"] is None
    assert row["cost_usd"] == 0.01


@pytest.mark.asyncio
async def test_registry_feeds_bound_accumulator():
    registry = MasterToolRegistry()

    def echo(q: str) -> str:
        return f"got:{q}"

    registry.register(
        "echo",
        "Echo the query back.",
        {
            "type": "object",
            "properties": {"q": {"type": "string"}},
            "required": ["q"],
            "additionalProperties": False,
        },
        echo,
    )
    assert current_turn_usage() is None
    await registry.execute("echo", {"q": "x"})
    usage, token = bind_turn_usage()
    try:
        await registry.execute("echo", {"q": "x"})
        await registry.execute("echo", {"q": "yy"})
    finally:
        reset_turn_usage(token)
    assert usage.tool_calls == 2
    assert usage.tool_result_chars == len("got:x") + len("got:yy")
    assert usage.tool_duration_ms >= 0


@pytest.mark.asyncio
async def test_finish_bot_turn_records_its_thread(monkeypatch, tmp_path):
    monkeypatch.setenv("VEYA_BOTS_ROOT", str(tmp_path / "bots-root"))
    monkeypatch.delenv("VEYA_TURN_QUEUE", raising=False)

    async def quick(**kwargs):
        return {"reply": "hi", "status": "completed"}

    monkeypatch.setattr("server.routes.product._run_product_task", quick)
    store = ConversationStore(tmp_path / "bots-root")
    result = await finish_bot_turn(
        bot=BotSpec(bot_id="beta", name="Beta"),
        start=BotTurnStart(
            bot_id="beta", task_id="t1", session_id="s1", trace_id="tr1"
        ),
        text="hello",
        user={"user_id": "u1", "username": "u"},
        conversations=store,
        thread_id="grp_9",
    )
    assert result.status == "completed"
    snapshot = thread_usage_snapshot()
    assert set(snapshot) == {"grp_9"}
    row = snapshot["grp_9"]
    assert row["turns"] == 1
    assert row["last_speaker"] == "beta"
    assert row["last_status"] == "completed"
    assert row["input_tokens"] is None


@pytest.mark.asyncio
async def test_thread_usage_endpoint_shape(monkeypatch):
    record_turn("grp_1", bot_id="beta", status="completed")
    from server.routes.bot_team import get_thread_usage

    body = await get_thread_usage(user={"user_id": "u1", "username": "u"})
    assert body["token_metering"] == "none"
    assert body["threads"]["grp_1"]["turns"] == 1
