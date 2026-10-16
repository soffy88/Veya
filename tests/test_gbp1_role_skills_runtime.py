"""Tests for VEYA_ROLE_SKILLS_RUNTIME skill loading in bot runtime."""

from __future__ import annotations

import pytest

from server.bot_team.turns import build_objective
from server.goal_run.bot_identity import BotSpec
from server.feature_flags import enabled


def test_role_skills_runtime_flag_default_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VEYA_ROLE_SKILLS_RUNTIME", raising=False)
    assert enabled("VEYA_ROLE_SKILLS_RUNTIME") is False


def test_build_objective_without_runtime_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VEYA_ROLE_SKILLS_RUNTIME", raising=False)
    bot = BotSpec(
        bot_id="bot_a",
        owner_id="u1",
        instructions="Instructions A",
        available_skill_ids=["preset:test-driven-development"],
    )
    prompt = build_objective(bot=bot, text="hello")
    assert "<available_skills>" not in prompt


def test_build_objective_with_runtime_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VEYA_ROLE_SKILLS_RUNTIME", "1")
    bot_a = BotSpec(
        bot_id="bot_a",
        owner_id="u1",
        instructions="Instructions A",
        available_skill_ids=["preset:test-driven-development"],
    )
    bot_b = BotSpec(
        bot_id="bot_b",
        owner_id="u1",
        instructions="Instructions B",
        available_skill_ids=[],
    )

    prompt_a = build_objective(bot=bot_a, text="hello")
    prompt_b = build_objective(bot=bot_b, text="hello")

    assert "<available_skills>" in prompt_a
    assert "test-driven-development" in prompt_a
    assert "<available_skills>" not in prompt_b
