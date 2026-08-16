"""server.project_understand 测试 — Understand 门禁 (docs/PROJECT_AGENT.md §7)。

测试不依赖真模型：understand() 的 LLM 依赖通过 `_llm` 注入桩替换。
"""

from __future__ import annotations

import pytest

from server.project_understand import (
    UnderstandResult,
    eager_act_result,
    force_confirm_ask,
    understand,
    validate_or_force_ask,
)

# ── validate_or_force_ask: 硬约束校验, 任何不自洽都安全降级为 ask ────────


def test_validate_act_with_questions_is_forced_to_ask():
    u = UnderstandResult(
        decision="act", confidence=0.9, interpretation="do the thing", questions=["huh?"]
    )
    out = validate_or_force_ask(u)
    assert out.decision == "ask"
    assert out.questions


def test_validate_act_with_empty_interpretation_is_forced_to_ask():
    u = UnderstandResult(decision="act", confidence=0.9, interpretation="")
    out = validate_or_force_ask(u)
    assert out.decision == "ask"


def test_validate_act_below_confidence_threshold_is_forced_to_ask():
    u = UnderstandResult(decision="act", confidence=0.5, interpretation="do the thing")
    out = validate_or_force_ask(u)
    assert out.decision == "ask"


def test_validate_act_above_threshold_passes_through():
    u = UnderstandResult(decision="act", confidence=0.9, interpretation="do the thing")
    out = validate_or_force_ask(u)
    assert out.decision == "act"
    assert out.interpretation == "do the thing"


def test_validate_ask_clamps_to_max_questions():
    u = UnderstandResult(
        decision="ask",
        confidence=0.1,
        interpretation="",
        questions=["a", "b", "c", "d", "e"],
    )
    out = validate_or_force_ask(u)
    assert 1 <= len(out.questions) <= 3


def test_validate_ask_with_no_questions_gets_default_question():
    u = UnderstandResult(decision="ask", confidence=0.1, interpretation="", questions=[])
    out = validate_or_force_ask(u)
    assert len(out.questions) == 1


# ── eager_act_result / force_confirm_ask: mode=act_eager / ask_only ────


def test_eager_act_result_is_always_act_with_no_questions():
    u = eager_act_result("修复登录 bug")
    assert u.decision == "act"
    assert u.questions == []
    assert "修复登录 bug" in u.interpretation


def test_force_confirm_ask_converts_act_to_ask():
    u = UnderstandResult(decision="act", confidence=0.95, interpretation="do the thing")
    out = force_confirm_ask(u)
    assert out.decision == "ask"
    assert len(out.questions) == 1
    assert "do the thing" in out.questions[0]


def test_force_confirm_ask_leaves_ask_untouched():
    u = UnderstandResult(decision="ask", confidence=0.1, interpretation="", questions=["q1"])
    out = force_confirm_ask(u)
    assert out is u


# ── understand(): 解析 + 校验, LLM 通过 _llm 注入桩 ─────────────────────


@pytest.mark.asyncio
async def test_understand_parses_valid_json_act():
    async def _llm(system: str, user: str) -> str:
        return (
            '{"decision": "act", "confidence": 0.9, "interpretation": "create hello.py", '
            '"assumptions": [], "questions": [], "risk_flags": [], "reasons": []}'
        )

    out = await understand("create hello.py", "", _llm=_llm)
    assert out.decision == "act"
    assert out.interpretation == "create hello.py"


@pytest.mark.asyncio
async def test_understand_parses_valid_json_ask():
    async def _llm(system: str, user: str) -> str:
        return (
            '{"decision": "ask", "confidence": 0.3, "interpretation": "", '
            '"assumptions": [], "questions": ["导出成什么格式?"], "risk_flags": [], '
            '"reasons": ["ambiguous_scope"]}'
        )

    out = await understand("导出数据", "", _llm=_llm)
    assert out.decision == "ask"
    assert out.questions == ["导出成什么格式?"]


@pytest.mark.asyncio
async def test_understand_tolerates_fenced_json_with_surrounding_text():
    async def _llm(system: str, user: str) -> str:
        return (
            "Sure, here is the JSON:\n```json\n"
            '{"decision": "act", "confidence": 0.8, "interpretation": "x", '
            '"assumptions": [], "questions": [], "risk_flags": [], "reasons": []}'
            "\n```\n"
        )

    out = await understand("do x", "", _llm=_llm)
    assert out.decision == "act"


@pytest.mark.asyncio
async def test_understand_unparseable_output_degrades_to_ask():
    async def _llm(system: str, user: str) -> str:
        return "I'm not sure what you mean by that."

    out = await understand("随便什么", "", _llm=_llm)
    assert out.decision == "ask"
    assert out.questions


@pytest.mark.asyncio
async def test_understand_llm_exception_degrades_to_ask_not_raised():
    async def _llm(system: str, user: str) -> str:
        raise RuntimeError("gateway timeout")

    out = await understand("随便什么", "", _llm=_llm)
    assert out.decision == "ask"


@pytest.mark.asyncio
async def test_understand_act_result_still_goes_through_hard_constraint_validation():
    """LLM 判定 act 但带了 questions（不自洽）——理解层内部也要兜底降级。"""

    async def _llm(system: str, user: str) -> str:
        return (
            '{"decision": "act", "confidence": 0.9, "interpretation": "x", '
            '"assumptions": [], "questions": ["huh?"], "risk_flags": [], "reasons": []}'
        )

    out = await understand("do x", "", _llm=_llm)
    assert out.decision == "ask"


@pytest.mark.asyncio
async def test_understand_passes_chain_and_memory_into_prompt():
    seen: dict = {}

    async def _llm(system: str, user: str) -> str:
        seen["user"] = user
        return (
            '{"decision": "ask", "confidence": 0.1, "interpretation": "", '
            '"assumptions": [], "questions": ["q"], "risk_flags": [], "reasons": []}'
        )

    chain = [{"request": "做个导出", "interpretation": "", "questions": ["格式?"]}]
    await understand("只要 CSV", "## Project memory\nsome state", chain, _llm=_llm)

    assert "做个导出" in seen["user"]
    assert "格式?" in seen["user"]
    assert "some state" in seen["user"]
    assert "只要 CSV" in seen["user"]
