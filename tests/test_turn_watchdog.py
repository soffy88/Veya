"""Stall watchdog: a turn past its deadline is stalled, noted, never retried."""

from __future__ import annotations

import asyncio

import pytest

from server.bot_team.conversations import ConversationStore
from server.bot_team.turn_watchdog import (
    DEFAULT_TURN_TIMEOUT_S,
    stall_note,
    turn_deadline,
    turn_timeout_s,
)
from server.bot_team.turns import BotTurnStart, finish_bot_turn
from server.goal_run.bot_identity import BotSpec


def test_timeout_parsing(monkeypatch):
    monkeypatch.delenv("VEYA_TURN_TIMEOUT_S", raising=False)
    assert turn_timeout_s() == DEFAULT_TURN_TIMEOUT_S
    monkeypatch.setenv("VEYA_TURN_TIMEOUT_S", "30")
    assert turn_timeout_s() == 30.0
    monkeypatch.setenv("VEYA_TURN_TIMEOUT_S", "0")
    assert turn_timeout_s() is None
    monkeypatch.setenv("VEYA_TURN_TIMEOUT_S", "-5")
    assert turn_timeout_s() is None
    monkeypatch.setenv("VEYA_TURN_TIMEOUT_S", "not-a-number")
    assert turn_timeout_s() == DEFAULT_TURN_TIMEOUT_S


def test_stall_note_text():
    note = stall_note(600.0)
    assert "600" in note and "stalled" in note and "没有重试" in note


@pytest.mark.asyncio
async def test_deadline_expires(monkeypatch):
    monkeypatch.setenv("VEYA_TURN_TIMEOUT_S", "0.05")
    with pytest.raises(TimeoutError):
        async with turn_deadline() as scope:
            assert scope is not None
            await asyncio.sleep(60)
    assert scope is not None and scope.expired()


async def _run_turn(monkeypatch, tmp_path, task, timeout):
    monkeypatch.setenv("VEYA_BOTS_ROOT", str(tmp_path / "bots-root"))
    monkeypatch.setenv("VEYA_TURN_TIMEOUT_S", timeout)
    monkeypatch.delenv("VEYA_TURN_QUEUE", raising=False)
    monkeypatch.setattr("server.routes.product._run_product_task", task)
    store = ConversationStore(tmp_path / "bots-root")
    result = await finish_bot_turn(
        bot=BotSpec(bot_id="beta", name="Beta"),
        start=BotTurnStart(
            bot_id="beta", task_id="t1", session_id="s1", trace_id="tr1"
        ),
        text="hello",
        user={"user_id": "u1", "username": "u"},
        conversations=store,
    )
    return result, store


@pytest.mark.asyncio
async def test_hanging_turn_is_stalled_not_retried(monkeypatch, tmp_path):
    calls = 0

    async def hang(**kwargs):
        nonlocal calls
        calls += 1
        await asyncio.sleep(60)
        return {"reply": "too late", "status": "completed"}

    result, store = await _run_turn(monkeypatch, tmp_path, hang, "0.05")
    assert result.status == "stalled"
    assert result.reply.startswith("（本轮等待超过 0.05 秒")
    assert calls == 1
    messages = store.list("beta")
    assert messages and messages[-1]["text"] == result.reply


@pytest.mark.asyncio
async def test_inner_timeout_error_is_not_a_stall(monkeypatch, tmp_path):
    async def fail_fast(**kwargs):
        raise TimeoutError("tool timed out")

    with pytest.raises(TimeoutError, match="tool timed out"):
        await _run_turn(monkeypatch, tmp_path, fail_fast, "60")


@pytest.mark.asyncio
async def test_quick_turn_unaffected(monkeypatch, tmp_path):
    async def quick(**kwargs):
        return {"reply": "hi", "status": "completed"}

    result, _store = await _run_turn(monkeypatch, tmp_path, quick, "60")
    assert result.status == "completed"
    assert result.reply == "hi"
