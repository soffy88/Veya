"""server.goal_run.plan_review — 计划前置双轴审查(oh-my-openagent 编排系统内化)。

对标 oh-my-openagent 的 orchestration 三层架构(见 memory
project_veya_pi_gap_audit): 计划(Prometheus)必须同时拿到 Momus(高精度评审)
和 Oracle(独立评审)两个 OKAY 才能进执行层，任一方 REJECT 就打回。goal_run
的 G1 Plan 生成任务图之后、G2 执行之前此前没有这道闸门——本模块补上。

跟 code_review.py 的事后双轴(Standards/Spec, advisory only)是两个不同的
插入点、不同的风险取舍：这里审的是**执行前的计划本身**，拦下来的成本是
"还没开始烧执行预算就发现问题", 比事后审更值——所以这里做成**真正的门禁**
(reject 就拦), 不是纯 advisory。

两轴: **Feasibility**(这个任务图真的能达成目标吗——依赖断裂/步骤缺失/循环)
和 **Safety**(有没有任务明显超出目标范围/有破坏性风险)。任一轴明确判
reject 就拦(不要求两轴都拒——各自查不同的东西, 要求同时拒等于门禁形同虚设)。
LLM 调用失败/无法解析一律放行(fail open, 不能让基础设施故障变成任务卡死),
只有真正拿到明确 reject 判断才拦。
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Awaitable, Callable
from typing import Any

from veya.llm import llm_call

_STUB_MARKER = "LLM provider not configured"

_FEASIBILITY_SYSTEM_PROMPT = """You review a generated task plan for whether it can \
actually reach the stated goal. Check: are there missing steps a competent \
engineer would need, do the dependency edges make sense (no gaps, no cycles), \
does finishing every task actually add up to the goal being met. This is not \
a safety review — do not comment on risk, only on whether the plan works.

Respond with ONLY a JSON object, no other text:
{"verdict": "approve" | "reject", "concerns": ["..."], "reasoning": "one paragraph"}"""

_SAFETY_SYSTEM_PROMPT = """You review a generated task plan for scope and risk. \
Check: does any task look destructive, irreversible, or clearly outside what \
the goal asked for (scope creep). This is not a feasibility review — do not \
comment on whether the plan works, only on whether it is safe and in scope.

Respond with ONLY a JSON object, no other text:
{"verdict": "approve" | "reject", "concerns": ["..."], "reasoning": "one paragraph"}"""


def _render_tasks(tasks: list[dict[str, Any]]) -> str:
    lines = []
    for t in tasks:
        lines.append(f"- {t.get('id')}: {t.get('title')}")
        lines.append(f"  instruction: {t.get('instruction')}")
        lines.append(f"  acceptance: {t.get('acceptance')}")
        lines.append(f"  depends_on: {t.get('depends_on')}")
    return "\n".join(lines) or "(no tasks)"


def _parse_verdict(raw: str) -> dict[str, Any]:
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        return {
            "verdict": "approve",
            "concerns": [],
            "reasoning": f"未能解析模型输出, 放行: {raw[:300]}",
        }
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return {
            "verdict": "approve",
            "concerns": [],
            "reasoning": f"模型输出非合法 JSON, 放行: {raw[:300]}",
        }
    verdict = data.get("verdict")
    if verdict not in ("approve", "reject"):
        return {
            "verdict": "approve",
            "concerns": [],
            "reasoning": f"未知 verdict {verdict!r}, 放行",
        }
    return {
        "verdict": verdict,
        "concerns": list(data.get("concerns") or []),
        "reasoning": str(data.get("reasoning") or ""),
    }


async def _call_axis(
    system_prompt: str,
    goal_text: str,
    tasks: list[dict[str, Any]],
    *,
    llm_call_fn: Callable[..., Awaitable[dict]],
) -> dict[str, Any]:
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": f"目标:\n{goal_text}\n\n任务图:\n{_render_tasks(tasks)}"},
    ]
    try:
        resp = await llm_call_fn(messages)
    except Exception as exc:
        return {
            "verdict": "approve",
            "concerns": [],
            "reasoning": f"LLM 调用异常, 放行: {type(exc).__name__}: {exc}",
        }
    content = ""
    if isinstance(resp, dict):
        choices = resp.get("choices") or []
        if choices:
            content = (choices[0].get("message") or {}).get("content") or ""
    if not content or _STUB_MARKER in content:
        return {"verdict": "approve", "concerns": [], "reasoning": "LLM 未配置(stub 回落), 放行"}
    return _parse_verdict(content)


async def dual_axis_plan_review(
    *,
    goal_text: str,
    tasks: list[dict[str, Any]],
    llm_call_fn: Callable[..., Awaitable[dict]] = llm_call,
) -> dict[str, Any]:
    """并行跑 Feasibility/Safety 两轴, 任一轴 reject 就 blocked=True。空任务图直接放行。"""
    if not tasks:
        empty = {"verdict": "approve", "concerns": [], "reasoning": "空任务图, 无需审查"}
        return {"feasibility": empty, "safety": empty, "blocked": False}
    feasibility, safety = await asyncio.gather(
        _call_axis(_FEASIBILITY_SYSTEM_PROMPT, goal_text, tasks, llm_call_fn=llm_call_fn),
        _call_axis(_SAFETY_SYSTEM_PROMPT, goal_text, tasks, llm_call_fn=llm_call_fn),
    )
    blocked = feasibility["verdict"] == "reject" or safety["verdict"] == "reject"
    return {"feasibility": feasibility, "safety": safety, "blocked": blocked}
