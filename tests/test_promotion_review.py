"""server.promotion_review 测试(CandidateLearning 双轴 Promotion 审查, P4 落地,
见 docs/dev/rfc-01-vaom.md)。跟 tests/goal_run/test_plan_review.py 同一套结构。
"""

from __future__ import annotations

import pytest

from server.promotion_review import dual_axis_promotion_review

_CLAIM = "先跑迁移脚本再动代码"
_EVIDENCE = ["migrate-then-code 让 auth 重构零回归", "migrate-then-code 提前发现字段冲突"]


def _llm_returning(content: str):
    async def fake(messages, **kwargs):
        return {"choices": [{"message": {"role": "assistant", "content": content}}]}

    return fake


@pytest.mark.asyncio
async def test_both_approve_not_blocked():
    llm = _llm_returning('{"verdict": "approve", "concerns": [], "reasoning": "ok"}')
    report = await dual_axis_promotion_review(claim=_CLAIM, evidence=_EVIDENCE, llm_call_fn=llm)
    assert report["blocked"] is False
    assert report["value"]["verdict"] == "approve"
    assert report["safety"]["verdict"] == "approve"


@pytest.mark.asyncio
async def test_value_reject_blocks_even_if_safety_approves():
    async def routed(messages, **kwargs):
        system = messages[0]["content"]
        if "evidence justifies promotion" in system or "genuinely supports" in system:
            return {
                "choices": [
                    {
                        "message": {
                            "content": '{"verdict": "reject", "concerns": ["证据太薄弱"], "reasoning": "x"}'
                        }
                    }
                ]
            }
        return {
            "choices": [
                {"message": {"content": '{"verdict": "approve", "concerns": [], "reasoning": "y"}'}}
            ]
        }

    report = await dual_axis_promotion_review(claim=_CLAIM, evidence=_EVIDENCE, llm_call_fn=routed)
    assert report["blocked"] is True
    assert report["value"]["verdict"] == "reject"
    assert report["safety"]["verdict"] == "approve"


@pytest.mark.asyncio
async def test_safety_reject_blocks_even_if_value_approves():
    async def routed(messages, **kwargs):
        system = messages[0]["content"]
        if "over-applied" in system or "safe" in system:
            return {
                "choices": [
                    {
                        "message": {
                            "content": '{"verdict": "reject", "concerns": ["会过度泛化"], "reasoning": "x"}'
                        }
                    }
                ]
            }
        return {
            "choices": [
                {"message": {"content": '{"verdict": "approve", "concerns": [], "reasoning": "y"}'}}
            ]
        }

    report = await dual_axis_promotion_review(claim=_CLAIM, evidence=_EVIDENCE, llm_call_fn=routed)
    assert report["blocked"] is True


@pytest.mark.asyncio
async def test_empty_evidence_never_blocks_without_llm_call():
    calls = []

    async def counting_llm(messages, **kwargs):
        calls.append(1)
        return {"choices": [{"message": {"content": "{}"}}]}

    report = await dual_axis_promotion_review(claim=_CLAIM, evidence=[], llm_call_fn=counting_llm)
    assert report["blocked"] is False
    assert calls == []


@pytest.mark.asyncio
async def test_llm_exception_fails_open_not_blocked():
    async def boom(messages, **kwargs):
        raise RuntimeError("provider down")

    report = await dual_axis_promotion_review(claim=_CLAIM, evidence=_EVIDENCE, llm_call_fn=boom)
    assert report["blocked"] is False
    assert report["value"]["verdict"] == "approve"
    assert report["safety"]["verdict"] == "approve"


@pytest.mark.asyncio
async def test_unparseable_output_fails_open():
    llm = _llm_returning("not json at all")
    report = await dual_axis_promotion_review(claim=_CLAIM, evidence=_EVIDENCE, llm_call_fn=llm)
    assert report["blocked"] is False


@pytest.mark.asyncio
async def test_stub_marker_fails_open():
    llm = _llm_returning("LLM provider not configured")
    report = await dual_axis_promotion_review(claim=_CLAIM, evidence=_EVIDENCE, llm_call_fn=llm)
    assert report["blocked"] is False
