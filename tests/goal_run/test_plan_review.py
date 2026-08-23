"""goal_run 计划前置双轴审查(oh-my-openagent orchestration 内化，见 memory
project_veya_pi_gap_audit)。

跟 code_review.py 的事后 advisory 双轴不同——这里是真正的门禁: Feasibility/
Safety 任一轴明确 reject 就拦, 不要求两轴都拒。LLM 失败/无法解析一律放行
(fail open)。
"""

from __future__ import annotations

import pytest

from server.goal_run.plan_review import dual_axis_plan_review

_TASKS = [
    {
        "id": "T1",
        "title": "写数据库迁移",
        "instruction": "加个 users 表",
        "acceptance": ["表存在"],
        "depends_on": [],
    },
    {
        "id": "T2",
        "title": "写 API",
        "instruction": "暴露 CRUD 接口",
        "acceptance": ["curl 能拿到 200"],
        "depends_on": ["T1"],
    },
]


def _llm_returning(content: str):
    async def fake(messages, **kwargs):
        return {"choices": [{"message": {"role": "assistant", "content": content}}]}

    return fake


@pytest.mark.asyncio
async def test_both_approve_not_blocked():
    llm = _llm_returning('{"verdict": "approve", "concerns": [], "reasoning": "ok"}')
    report = await dual_axis_plan_review(
        goal_text="build a CRUD API", tasks=_TASKS, llm_call_fn=llm
    )
    assert report["blocked"] is False
    assert report["feasibility"]["verdict"] == "approve"
    assert report["safety"]["verdict"] == "approve"


@pytest.mark.asyncio
async def test_feasibility_reject_blocks_even_if_safety_approves():
    async def routed(messages, **kwargs):
        system = messages[0]["content"]
        if "Feasibility" in system or "reach the stated goal" in system:
            return {
                "choices": [
                    {
                        "message": {
                            "content": '{"verdict": "reject", "concerns": ["缺少认证步骤"], "reasoning": "x"}'
                        }
                    }
                ]
            }
        return {
            "choices": [
                {"message": {"content": '{"verdict": "approve", "concerns": [], "reasoning": "y"}'}}
            ]
        }

    report = await dual_axis_plan_review(
        goal_text="build a CRUD API", tasks=_TASKS, llm_call_fn=routed
    )
    assert report["blocked"] is True
    assert report["feasibility"]["verdict"] == "reject"
    assert report["safety"]["verdict"] == "approve"


@pytest.mark.asyncio
async def test_safety_reject_blocks_even_if_feasibility_approves():
    async def routed(messages, **kwargs):
        system = messages[0]["content"]
        if "scope and risk" in system or "Safety" in system:
            return {
                "choices": [
                    {
                        "message": {
                            "content": '{"verdict": "reject", "concerns": ["会删库"], "reasoning": "x"}'
                        }
                    }
                ]
            }
        return {
            "choices": [
                {"message": {"content": '{"verdict": "approve", "concerns": [], "reasoning": "y"}'}}
            ]
        }

    report = await dual_axis_plan_review(
        goal_text="build a CRUD API", tasks=_TASKS, llm_call_fn=routed
    )
    assert report["blocked"] is True


@pytest.mark.asyncio
async def test_empty_tasks_never_blocks_without_llm_call():
    calls = []

    async def counting_llm(messages, **kwargs):
        calls.append(1)
        return {"choices": [{"message": {"content": "{}"}}]}

    report = await dual_axis_plan_review(goal_text="x", tasks=[], llm_call_fn=counting_llm)
    assert report["blocked"] is False
    assert calls == []


@pytest.mark.asyncio
async def test_llm_exception_fails_open_not_blocked():
    async def boom(messages, **kwargs):
        raise RuntimeError("down")

    report = await dual_axis_plan_review(goal_text="x", tasks=_TASKS, llm_call_fn=boom)
    assert report["blocked"] is False


@pytest.mark.asyncio
async def test_stub_response_fails_open_not_blocked():
    llm = _llm_returning("LLM provider not configured — this is a shim response.")
    report = await dual_axis_plan_review(goal_text="x", tasks=_TASKS, llm_call_fn=llm)
    assert report["blocked"] is False


@pytest.mark.asyncio
async def test_malformed_json_fails_open_not_blocked():
    llm = _llm_returning("I refuse to output JSON.")
    report = await dual_axis_plan_review(goal_text="x", tasks=_TASKS, llm_call_fn=llm)
    assert report["blocked"] is False
