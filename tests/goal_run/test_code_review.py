"""goal_run 双轴代码审查(mattpocock/skills code-review 内化，见 memory
project_veya_pi_gap_audit)。

Standards 轴(仓库约定, 12坏味道地板) 和 Spec 轴(是不是做了被要求的事)从不
合并结论——各自独立 LLM 调用, 报告分开。
"""

from __future__ import annotations

import pytest

from server.goal_run.code_review import dual_axis_review, review_spec, review_standards

_DIFF = "diff --git a/foo.py b/foo.py\n+def helper(a, b, c, d, e):\n+    return a + b + c + d + e\n"


def _llm_returning(content: str):
    async def fake(messages, **kwargs):
        return {"choices": [{"message": {"role": "assistant", "content": content}}]}

    return fake


@pytest.mark.asyncio
async def test_review_standards_parses_findings():
    llm = _llm_returning(
        '{"findings": [{"rule": "Data Clumps", "detail": "五个参数总一起出现", "hunk": "def helper(...)"}], '
        '"worst": "考虑把参数收成一个对象"}'
    )
    result = await review_standards(_DIFF, llm_call_fn=llm)
    assert result["findings"][0]["rule"] == "Data Clumps"
    assert "参数" in result["worst"]


@pytest.mark.asyncio
async def test_review_spec_cites_acceptance():
    llm = _llm_returning(
        '{"findings": [{"requirement": "返回值必须是 dict", "detail": "helper 返回 int"}], '
        '"worst": "返回类型不对"}'
    )
    result = await review_spec(
        _DIFF,
        task_instruction="写一个 helper 函数",
        acceptance=["返回值必须是 dict"],
        llm_call_fn=llm,
    )
    assert result["findings"][0]["requirement"] == "返回值必须是 dict"


@pytest.mark.asyncio
async def test_empty_diff_skips_both_axes_without_llm_call():
    calls = []

    async def counting_llm(messages, **kwargs):
        calls.append(1)
        return {"choices": [{"message": {"role": "assistant", "content": "{}"}}]}

    report = await dual_axis_review(
        diff_text="", task_instruction="x", acceptance=[], llm_call_fn=counting_llm
    )
    assert calls == []
    assert report["standards"]["unscanned_reason"] == "diff 为空"
    assert report["spec"]["unscanned_reason"] == "diff 为空"


@pytest.mark.asyncio
async def test_dual_axis_review_never_blends_axes():
    async def routed_llm(messages, **kwargs):
        system = messages[0]["content"]
        if "Standards" in system or "Fowler" in system:
            return {
                "choices": [
                    {"message": {"content": '{"findings": [], "worst": "standards issue"}'}}
                ]
            }
        return {"choices": [{"message": {"content": '{"findings": [], "worst": "spec issue"}'}}]}

    report = await dual_axis_review(
        diff_text=_DIFF, task_instruction="do X", acceptance=["X done"], llm_call_fn=routed_llm
    )
    assert report["standards"]["worst"] == "standards issue"
    assert report["spec"]["worst"] == "spec issue"
    assert "blended" not in report
    assert set(report.keys()) == {"standards", "spec"}  # 没有第三个"综合"字段


@pytest.mark.asyncio
async def test_standards_axis_prefers_repo_doc_over_baseline():
    llm = _llm_returning('{"findings": [], "worst": null}')
    calls = []

    async def capturing_llm(messages, **kwargs):
        calls.append(messages[1]["content"])
        return await llm(messages, **kwargs)

    await review_standards(
        _DIFF, standards_doc="Never use global state.", llm_call_fn=capturing_llm
    )
    assert "Never use global state." in calls[0]


@pytest.mark.asyncio
async def test_llm_exception_degrades_gracefully_not_crash():
    async def boom(messages, **kwargs):
        raise RuntimeError("down")

    result = await review_standards(_DIFF, llm_call_fn=boom)
    assert result["findings"] == []
    assert "down" in result["unscanned_reason"]


@pytest.mark.asyncio
async def test_stub_response_is_unscanned_not_fabricated_findings():
    llm = _llm_returning("LLM provider not configured — this is a shim response.")
    result = await review_spec(_DIFF, task_instruction="x", acceptance=[], llm_call_fn=llm)
    assert result["findings"] == []
    assert result["worst"] is None
