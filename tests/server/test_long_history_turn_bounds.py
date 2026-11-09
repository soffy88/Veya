"""Long-history turns must stay bounded before the main answer starts.

Two failure modes are covered here:

* context compaction renders the whole older window into one summary request
  and waited for it with no timeout — a long history plus a slow upstream made
  a turn spend minutes *before* the answer call;
* a model-decision timeout propagated out of the GoalRun leaf, so the runner
  retried the whole 120 s decision (~4 x 120 s observed), which is longer than
  the 300 s client turn timeout.

Both fixes only bound latency/failure handling; they do not change routing,
tool surface, or the answer itself.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from typing import Any

import pytest

from server.coordinator_master import MasterCoordinator
from server.goal_run.canonical_worker import (
    _MODEL_TIMEOUT_REPLY,
    CanonicalWorkerAdapter,
)


class _HistoryStore:
    async def load(self, *_args: Any, **_kwargs: Any) -> list[dict]:
        return []

    async def save(self, *_args: Any, **_kwargs: Any) -> None:
        return None


class _MemoryStore:
    async def retrieve(self, *_args: Any, **_kwargs: Any) -> list[dict]:
        return []

    async def add(self, *_args: Any, **_kwargs: Any) -> str:
        return ""


def _coordinator(**kwargs: Any) -> MasterCoordinator:
    return MasterCoordinator(history_store=_HistoryStore(), memory_store=_MemoryStore(), **kwargs)


def _long_history(turns: int, content_chars: int = 4000) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = [{"role": "system", "content": "system"}]
    for index in range(turns):
        messages.append({"role": "user", "content": f"q{index} " + "x" * content_chars})
        messages.append({"role": "assistant", "content": f"a{index} " + "y" * content_chars})
    return messages


@pytest.mark.asyncio
async def test_compaction_summary_input_is_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VEYA_COMPACT_SUMMARY_MAX_CHARS", "1000")
    monkeypatch.delenv("VEYA_COMPACT_SUMMARY_TIMEOUT_S", raising=False)
    coord = _coordinator()
    seen: list[str] = []

    async def _capture(messages: list[dict[str, Any]]) -> dict[str, Any]:
        seen.append(str(messages[-1].get("content") or ""))
        return {"choices": [{"message": {"content": "summary"}}]}

    coord._compact_llm_fn = _capture
    summary = await coord._summarize_for_compaction(_long_history(10))

    assert summary == "summary"
    assert seen, "summary llm was not called"
    assert "[较早内容已截断]" in seen[0]
    # The rendered conversation is capped; the small instruction wrapper is not
    # part of the cap but is a fixed, tiny prefix.
    assert len(seen[0]) < 1200


@pytest.mark.asyncio
async def test_compaction_summary_timeout_skips_and_keeps_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VEYA_COMPACT_SUMMARY_TIMEOUT_S", "0.05")
    monkeypatch.setenv("VEYA_CONTEXT_TOKEN_BUDGET", "100")
    monkeypatch.setenv("VEYA_CONTEXT_COMPACT_TRIGGER_RATIO", "0")
    monkeypatch.setenv("VEYA_CONTEXT_COMPACT_TAIL_MSGS", "1")
    coord = _coordinator()
    history = _long_history(3)
    coord._agent._histories = {"s": history}
    before = [dict(message) for message in history]

    async def _slow(_messages: list[dict[str, Any]]) -> dict[str, Any]:
        await asyncio.sleep(1.0)
        return {"choices": [{"message": {"content": "too late"}}]}

    coord._compact_llm_fn = _slow
    started = time.monotonic()
    await coord._maybe_compact_history("s")
    elapsed = time.monotonic() - started

    assert elapsed < 0.5, f"compaction summary was not bounded: {elapsed:.2f}s"
    assert history == before, "a timed-out summary must not rewrite history"


@pytest.mark.asyncio
async def test_semantic_timeout_is_terminal_for_the_turn() -> None:
    class _Agent:
        async def semantic_step(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
            raise TimeoutError("model decision timed out")

    adapter = CanonicalWorkerAdapter(
        task_id="task-1",
        objective="2+3",
        semantic_agent=_Agent(),
        gateway_executor=object(),
    )
    state = SimpleNamespace(goal_id="goal-1", bot_id="bot-1", budget={})
    task = SimpleNamespace(id="task-1", instruction="2+3等于几")

    result = await adapter.execute_semantic_task(state, task)

    assert result.status == "completed"
    assert result.summary == _MODEL_TIMEOUT_REPLY
    assert result.stop_reason == "model_timeout"
    assert state.budget["reply"] == _MODEL_TIMEOUT_REPLY
