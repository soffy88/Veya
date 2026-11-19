"""Bot-runtime suite fixtures: hermetic executor health and offline LLM."""

from __future__ import annotations

import pytest

from tests._executor_health import _healthy_admission_executor  # noqa: F401 (autouse)


@pytest.fixture(autouse=True)
def _registered_executor_adapters() -> None:
    """Populate the substrate adapter registry for every bot-runtime test.

    Several modules imported ``register_default_adapters`` locally and others
    did not; the result depended on pytest import order (a passing sibling test
    left the global registry populated). Registering the real defaults here is
    deterministic and does not weaken admission: the executor registry and the
    health fixture still decide whether a substrate may be used.
    """
    from veya.remote.executor_adapters import register_default_adapters

    register_default_adapters()


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
