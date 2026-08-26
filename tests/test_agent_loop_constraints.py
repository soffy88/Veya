"""AgentLoop delegation budget/deadline contracts (spec §14)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest


class _CostedLlm:
    async def complete(self, messages, **kwargs):
        return {
            "choices": [{"message": {"role": "assistant", "content": "done"}}],
            "cost_usd": 0.25,
        }


class _SlowLlm:
    async def complete(self, messages, **kwargs):
        import asyncio

        await asyncio.sleep(0.2)
        return {"choices": [{"message": {"role": "assistant", "content": "late"}}]}


@pytest.mark.asyncio
async def test_strict_chat_budget_is_hard_stop(tmp_path):
    from server.agent_loop_bridge import run_strict_chat

    result = await run_strict_chat(
        "budgeted task",
        llm=_CostedLlm(),
        budget_usd=0.10,
        max_rounds=3,
        kv_path=str(tmp_path / "budget.db"),
    )

    assert result["status"] == "failed"
    assert result["stop_kind"] == "budget_exceeded"
    assert result["cost_usd"] == 0.25
    assert result["tool_calls"] == []


@pytest.mark.asyncio
async def test_strict_chat_deadline_cancels_child(tmp_path):
    from server.agent_loop_bridge import run_strict_chat

    result = await run_strict_chat(
        "deadline task",
        llm=_SlowLlm(),
        deadline=datetime.now(UTC) + timedelta(milliseconds=20),
        max_rounds=3,
        kv_path=str(tmp_path / "deadline.db"),
    )

    assert result["status"] == "failed"
    assert result["stop_kind"] == "deadline_exceeded"
    assert "deadline" in result["error"]
