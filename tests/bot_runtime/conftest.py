"""Bot-runtime suite fixtures: hermetic executor health and offline LLM."""

from __future__ import annotations

import pytest

from tests._executor_health import _healthy_admission_executor  # noqa: F401 (autouse)


class _OfflineLLM:
    """Stand-in for the container default LLM: answers at once, no network.

    The bot runtime runs the AgentLoop and then the canonical GoalRun chain;
    the GoalRun is the completion authority, so these tests only need the
    loop to return promptly instead of reaching a real provider.
    """

    async def complete(self, messages, **kwargs):
        return {"role": "assistant", "content": "ok", "tool_calls": []}


@pytest.fixture(autouse=True)
def _offline_default_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.bot_runtime import engine

    real = engine._as_llm_client

    def _client(llm):
        return _OfflineLLM() if llm is None else real(llm)

    monkeypatch.setattr(engine, "_as_llm_client", _client)
