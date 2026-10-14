"""Unit and contract tests for VEYA_SKILL_PROGRESSIVE (clean-room borrow)."""

from __future__ import annotations

import pytest

from server.bot_team.turns import build_objective
from server.goal_run.bot_identity import BotSpec
from server.skill_progressive import (
    FLAG_NAME,
    compare_skills_tokens,
    get_skill_info,
    is_skill_progressive_enabled,
    read_skill,
)
from server.tool_registry import master_tools


def test_flag_registered_default_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(FLAG_NAME, raising=False)
    monkeypatch.delenv("VEYA_SKILL_PROGRESSIVE_DISCLOSURE", raising=False)
    assert is_skill_progressive_enabled() is False


def test_flag_turn_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(FLAG_NAME, "1")
    assert is_skill_progressive_enabled() is True


def test_read_skill_from_presets() -> None:
    # W15 preset skill
    info = get_skill_info("preset:test-driven-development")
    assert "test-driven-development" in info["id"]
    assert info["name"]
    assert info["summary"]
    assert len(info["content"]) > 100

    content = read_skill("preset:test-driven-development")
    assert "# Skill:" in content
    assert "Overview" in content or "test" in content.lower()


def test_read_skill_unknown_returns_error() -> None:
    res = read_skill("non-existent-skill-xyz")
    assert "Error:" in res
    assert "non-existent-skill-xyz" in res


def test_tool_registry_has_read_skill() -> None:
    assert master_tools.has("read_skill")
    assert master_tools.describe("read_skill")
    assert master_tools.is_parallel_safe("read_skill")
    schemas = master_tools.get_all_schemas()
    assert any(s["function"]["name"] == "read_skill" for s in schemas)


def test_build_objective_without_flag_preserves_behavior(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(FLAG_NAME, raising=False)
    monkeypatch.delenv("VEYA_SKILL_PROGRESSIVE_DISCLOSURE", raising=False)

    bot = BotSpec(
        bot_id="b1",
        owner_id="u1",
        instructions="You are a coder.",
        description="Write code",
        available_skill_ids=["preset:test-driven-development", "preset:code-review-and-quality"],
    )
    prompt = build_objective(bot=bot, text="Fix the bug")
    assert "<available_skills>" not in prompt
    assert "You are a coder." in prompt
    assert "Fix the bug" in prompt


def test_build_objective_with_progressive_flag_injects_summary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(FLAG_NAME, "1")

    bot = BotSpec(
        bot_id="b1",
        owner_id="u1",
        instructions="You are a coder.",
        description="Write code",
        available_skill_ids=["preset:test-driven-development", "preset:code-review-and-quality"],
    )
    prompt = build_objective(bot=bot, text="Fix the bug")
    assert "<available_skills>" in prompt
    assert "read_skill(skill_id)" in prompt
    assert "preset:test-driven-development" in prompt
    assert "preset:code-review-and-quality" in prompt
    # Full body should NOT be in the prompt!
    assert "## Overview" not in prompt


def test_token_comparison_measurement() -> None:
    skills = [
        "preset:test-driven-development",
        "preset:code-review-and-quality",
        "preset:planning-and-task-breakdown",
    ]
    base_prompt = "You are a senior full-stack engineer. Follow our team engineering standards."
    result = compare_skills_tokens(skills, base_prompt=base_prompt)

    assert result["skill_count"] == 3
    assert result["tokens_before"] > result["tokens_after"]
    assert result["tokens_saved"] > 0
    assert result["reduction_percentage"] > 50.0  # Significant reduction!
    print(
        f"\n[Token Measurement] Before: {result['tokens_before']} tokens, After: {result['tokens_after']} tokens, Saved: {result['tokens_saved']} tokens ({result['reduction_percentage']}%)"
    )
