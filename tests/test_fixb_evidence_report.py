"""tests/test_fixb_evidence_report.py — FIXB tests for Evidence Report & Default Bot Plan Review Gate.

Tests:
1. Default Bot (veya-default) single-task turn skips dual-axis plan review (no longer blocks with "这一步没有完成").
2. Prompt injection in build_objective injects 来源/猜测/跳过 requirements for BOTH default bot and role bot when VEYA_EVIDENCE_REPORT=1.
3. Stable three-section output formatting for both default bot and role bot.
4. Structured "猜测" extraction from trajectory (unverified assertions / default values) without relying on keyword "assume".
"""

from __future__ import annotations

from pathlib import Path

import pytest

from server.bot_team.turns import build_objective, format_evidence_report
from server.goal_run.bot_identity import DEFAULT_BOT_ID, BotSpec
from server.goal_run.models import GoalRunState, TaskNode, TaskStatus
from server.goal_run.runner import _run_plan_review_gate
from server.supervisor.reporting import _extract_guesses

# ---------------------------------------------------------------------------
# Test 1: Default Bot single-task chat turn skips plan review
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_default_bot_single_task_skips_plan_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Ensure veya-default bot turn skips plan review gate for single task."""
    state = GoalRunState(
        goal_id="g_default_chat",
        goal_text="请简要介绍 2026 年 AI Agent 发展趋势，并列出来源与未证实猜测、跳过的部分。",
        bot_id=DEFAULT_BOT_ID,
    )
    state.tasks["task_chat"] = TaskNode(
        id="task_chat",
        title="AI Agent 发展趋势",
        instruction="请简要介绍 2026 年 AI Agent 发展趋势",
        acceptance=[],
        depends_on=[],
        assignee="builtin",
    )

    async def mock_dual_axis_plan_review(**kwargs):
        raise AssertionError("dual_axis_plan_review must NOT be called for single-task bot turn")

    monkeypatch.setattr(
        "server.goal_run.plan_review.dual_axis_plan_review", mock_dual_axis_plan_review
    )

    # Should return None (allowed to proceed, not blocked into awaiting_user)
    resp = await _run_plan_review_gate(state, state.goal_text, str(tmp_path))
    assert resp is None
    assert state.plan_review is not None
    assert state.plan_review.get("skipped") is True
    assert "single-task bot turn" in state.plan_review.get("reason", "")


# ---------------------------------------------------------------------------
# Test 2: Prompt injection in build_objective for default bot and role bots
# ---------------------------------------------------------------------------


def test_build_objective_injects_evidence_requirements_for_all_bots(
    monkeypatch: pytest.MonkeyPatch,
):
    """When VEYA_EVIDENCE_REPORT=1, build_objective injects 来源/猜测/跳过 for any bot."""
    monkeypatch.setenv("VEYA_EVIDENCE_REPORT", "1")

    # Default bot
    default_bot = BotSpec(bot_id=DEFAULT_BOT_ID, owner_id="u1", name="Veya Default")
    prompt_default = build_objective(bot=default_bot, text="介绍新架构")
    assert "【来源】" in prompt_default
    assert "【猜测】" in prompt_default
    assert "【跳过】" in prompt_default

    # Role bot (e.g., researcher)
    role_bot = BotSpec(
        bot_id="researcher", owner_id="u1", name="Researcher", description="Deep researcher"
    )
    prompt_role = build_objective(bot=role_bot, text="调研报告")
    assert "【来源】" in prompt_role
    assert "【猜测】" in prompt_role
    assert "【跳过】" in prompt_role

    # Flag off: should not force the structured prompt
    monkeypatch.setenv("VEYA_EVIDENCE_REPORT", "0")
    prompt_off = build_objective(bot=default_bot, text="介绍新架构")
    assert "【来源】" not in prompt_off
    assert "【猜测】" not in prompt_off
    assert "【跳过】" not in prompt_off


# ---------------------------------------------------------------------------
# Test 3: Structured guesses extraction from trajectory (not keyword assume)
# ---------------------------------------------------------------------------


def test_structured_guesses_from_trajectory(tmp_path: Path):
    """Guesses extracted from task.assertions (weak/unverified) and defaults, not 'assume' keyword."""
    state = GoalRunState(goal_id="g_guess", goal_text="evaluate system")
    state.tasks["t1"] = TaskNode(
        id="t1",
        title="check config",
        instruction="check",
        acceptance=[],
        depends_on=[],
        assignee="worker",
        status=TaskStatus.completed,
        execute_result="Configuration applied without error",  # Does NOT contain 'assume'
        assertions=[
            {
                "id": "a1",
                "statement": "Database connection pool defaults to 10 connections",
                "status": "weak",
                "evidence_ids": [],
            },
            {
                "id": "a2",
                "statement": "Redis latency verified at 2ms",
                "status": "supported",
                "evidence_ids": ["ev_redis_1"],
            },
        ],
        evidence=[
            {
                "id": "ev_def",
                "kind": "unverified_assertion",
                "content": "Assumed timeout value 30s based on environment fallback",
            }
        ],
    )

    guesses = _extract_guesses(state)
    assert any("Database connection pool defaults to 10 connections" in g for g in guesses)
    assert any("Assumed timeout value 30s" in g for g in guesses)
    # The verified assertion (Redis latency) must NOT be listed under guesses
    assert not any("Redis latency verified" in g for g in guesses)


# ---------------------------------------------------------------------------
# Test 4: Stable 3-section output for default bot & role bot replies
# ---------------------------------------------------------------------------


def test_stable_three_section_bot_reply_formatting(monkeypatch: pytest.MonkeyPatch):
    """When VEYA_EVIDENCE_REPORT=1, format_evidence_report ensures 【来源】【猜测】【跳过】 sections exist."""
    monkeypatch.setenv("VEYA_EVIDENCE_REPORT", "1")

    state = GoalRunState(goal_id="g_reply", goal_text="test")
    state.tasks["t1"] = TaskNode(
        id="t1",
        title="t1",
        instruction="do t1",
        acceptance=[],
        depends_on=[],
        assignee="builtin",
        status=TaskStatus.completed,
        evidence=[{"content": "Read file docs/ARCHITECTURE_STABLE.md"}],
    )

    # Case A: Model replied with raw text without sections
    raw_reply = "这是 2026 年 Agent 发展趋势分析：自主多模型协同和确定性门禁逐渐成为主流。"
    formatted = format_evidence_report(raw_reply, state=state)
    assert "【来源】" in formatted
    assert "【猜测】" in formatted
    assert "【跳过】" in formatted
    assert "docs/ARCHITECTURE_STABLE.md" in formatted
    assert "（无未证实猜测）" in formatted
    assert "（无跳过项）" in formatted

    # Case B: Model already output sections
    already_formatted = (
        "分析结论如下。\n\n"
        "【来源】\n- docs/ARCHITECTURE_STABLE.md\n\n"
        "【猜测】\n- 未验证模型响应延迟\n\n"
        "【跳过】\n- 跳过第三方网络测试"
    )
    formatted2 = format_evidence_report(already_formatted, state=state)
    assert "docs/ARCHITECTURE_STABLE.md" in formatted2
    assert "未验证模型响应延迟" in formatted2
    assert "跳过第三方网络测试" in formatted2
