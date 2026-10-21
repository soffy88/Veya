"""Hark parity Phase 0: opt-in per-round compaction (VEYA_ROUND_COMPACTION)."""

from __future__ import annotations

from typing import Any

import pytest

from server.coordinator_master import MasterCoordinator, _RoundCompactionDriver


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


def _history(turns: int) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = [{"role": "system", "content": "system"}]
    for index in range(turns):
        messages.append({"role": "user", "content": f"question {index}"})
        messages.append({"role": "assistant", "content": f"answer {index}"})
    return messages


@pytest.fixture(autouse=True)
def _flags(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("VEYA_ROUND_COMPACTION", raising=False)
    monkeypatch.delenv("VEYA_ROUND_COMPACTION_EVERY", raising=False)
    yield


def test_flag_defaults_off_and_interval_defaults_to_six(monkeypatch) -> None:
    coord = _coordinator()
    assert coord._round_compaction_enabled is False
    assert coord._round_compaction_every == 6


def test_interval_is_configurable(monkeypatch) -> None:
    monkeypatch.setenv("VEYA_ROUND_COMPACTION", "1")
    monkeypatch.setenv("VEYA_ROUND_COMPACTION_EVERY", "3")
    coord = _coordinator()
    assert coord._round_compaction_enabled is True
    assert coord._round_compaction_every == 3


@pytest.mark.asyncio
async def test_disabled_flag_leaves_history_untouched(monkeypatch) -> None:
    coord = _coordinator()
    history = _history(10)
    coord._agent._histories = {"s": history}
    called: list[int] = []

    async def _summarize(_messages):
        called.append(1)
        return "summary"

    coord._summarize_for_compaction = _summarize
    before = [dict(message) for message in history]
    await coord._maybe_compact_rounds("s", rounds=7)
    assert history == before
    assert called == []


@pytest.mark.asyncio
async def test_enabled_flag_folds_old_rounds(monkeypatch) -> None:
    monkeypatch.setenv("VEYA_ROUND_COMPACTION", "1")
    monkeypatch.setenv("VEYA_ROUND_COMPACTION_TAIL_MSGS", "4")
    coord = _coordinator()
    history = _history(10)
    coord._agent._histories = {"s": history}

    async def _summarize(messages):
        assert messages
        return "earlier rounds summarised"

    coord._summarize_for_compaction = _summarize
    await coord._maybe_compact_rounds("s", rounds=7)

    assert history[0]["role"] == "system"
    assert any("[ROUND SUMMARY" in str(message.get("content")) for message in history)
    assert len(history) < 21
    assert history[-1] == {"role": "assistant", "content": "answer 9"}


@pytest.mark.asyncio
async def test_summary_failure_keeps_history(monkeypatch) -> None:
    monkeypatch.setenv("VEYA_ROUND_COMPACTION", "1")
    coord = _coordinator()
    history = _history(10)
    coord._agent._histories = {"s": history}
    before = [dict(message) for message in history]

    async def _summarize(_messages):
        raise RuntimeError("provider down")

    coord._summarize_for_compaction = _summarize
    await coord._maybe_compact_rounds("s", rounds=7)
    assert history == before


@pytest.mark.asyncio
async def test_empty_summary_keeps_history(monkeypatch) -> None:
    monkeypatch.setenv("VEYA_ROUND_COMPACTION", "1")
    coord = _coordinator()
    history = _history(10)
    coord._agent._histories = {"s": history}
    before = [dict(message) for message in history]

    async def _summarize(_messages):
        return "   "

    coord._summarize_for_compaction = _summarize
    await coord._maybe_compact_rounds("s", rounds=7)
    assert history == before


@pytest.mark.asyncio
async def test_driver_compacts_only_after_interval(monkeypatch) -> None:
    monkeypatch.setenv("VEYA_ROUND_COMPACTION", "1")
    monkeypatch.setenv("VEYA_ROUND_COMPACTION_EVERY", "2")
    coord = _coordinator()
    coord._agent._histories = {"s": _history(8)}
    compactions: list[int] = []

    async def _compact(_sid, *, rounds):
        compactions.append(rounds)

    coord._maybe_compact_rounds = _compact
    driver = _RoundCompactionDriver(coord, "s", None)
    for _ in range(6):
        await driver.pre_round()
    # Compacts at rounds 3 and 5 with interval 2.
    assert compactions == [3, 5]


@pytest.mark.asyncio
async def test_driver_delegates_to_inner() -> None:
    class _Inner:
        def __init__(self) -> None:
            self.pre = 0
            self.post = 0

        async def pre_round(self):
            self.pre += 1
            return "inner-context"

        async def post_round(self, outcome):
            self.post += 1

    coord = _coordinator()
    inner = _Inner()
    driver = _RoundCompactionDriver(coord, "s", inner)
    assert await driver.pre_round() == "inner-context"
    await driver.post_round({"cost_usd": 0})
    assert (inner.pre, inner.post) == (1, 1)


@pytest.mark.asyncio
async def test_driver_returns_round_context_without_inner() -> None:
    coord = _coordinator()
    context = await _RoundCompactionDriver(coord, "s", None).pre_round()
    assert context.quota_ok is True
