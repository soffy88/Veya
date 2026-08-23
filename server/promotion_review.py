"""server.promotion_review — CandidateLearning 的双轴 Promotion 审查。

跟 `server/goal_run/plan_review.py` 同一套结构(独立两轴、任一轴 reject 就拦、
LLM 失败 fail-open)——不合并/不复用 `plan_review.py` 里的私有函数(它是给
task plan 审的, prompt/输入形状不同), 沿用现成模式而不是抽象出共享基类, 跟
`code_review.py`/`plan_review.py` 两个既有实现各自独立同一套结构的既定风格
一致(见两个模块各自都有 `_call_axis`/`_parse_verdict`)。

两轴: **Value**(这条候选学习真的值得转正吗——证据支不支持它, 会不会只是
巧合/单次侥幸)和 **Safety**(转正后会不会带来风险——过度泛化到不该用的场景、
跟已有 verified 知识冲突)。这是 PR-23 CrossFamilyReviewer 的落地形式：真正
"跨家族"(不同模型走不同 provider)的独立性本轮未做, 只做到"两条独立 prompt
互不看对方推理"这一层——见 server/learning_engine.py 模块 docstring 的范围说明。
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Awaitable, Callable
from typing import Any

from veya.llm import llm_call

_STUB_MARKER = "LLM provider not configured"

_VALUE_SYSTEM_PROMPT = """You review a candidate learning proposed from repeated \
observations across multiple task episodes. You are given the claim and the \
evidence (statements from each episode that support it). Judge whether the \
evidence genuinely supports promoting this to a verified, reusable lesson — \
or whether it looks like coincidence, a one-off fluke, or evidence too thin \
to generalize from. Do not comment on risk or safety here, only on whether \
the evidence justifies promotion.

Respond with ONLY a JSON object, no other text:
{"verdict": "approve" | "reject", "concerns": ["..."], "reasoning": "one paragraph"}"""

_SAFETY_SYSTEM_PROMPT = """You review a candidate learning proposed from repeated \
observations across multiple task episodes, specifically for risk. Check: \
would promoting this to a verified, reusable lesson risk being over-applied \
to situations where it does not hold, or risk contradicting other established \
knowledge. Do not comment on whether the evidence is sufficient, only on \
whether promoting it is safe.

Respond with ONLY a JSON object, no other text:
{"verdict": "approve" | "reject", "concerns": ["..."], "reasoning": "one paragraph"}"""


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
    claim: str,
    evidence: list[str],
    *,
    llm_call_fn: Callable[..., Awaitable[dict]],
) -> dict[str, Any]:
    evidence_block = "\n".join(f"- {e}" for e in evidence) or "(无证据)"
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": f"候选claim:\n{claim}\n\n支持证据:\n{evidence_block}"},
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


async def dual_axis_promotion_review(
    *,
    claim: str,
    evidence: list[str],
    llm_call_fn: Callable[..., Awaitable[dict]] = llm_call,
) -> dict[str, Any]:
    """并行跑 Value/Safety 两轴, 任一轴 reject 就 blocked=True。无证据直接放行
    (没东西可评, 跟 plan_review 空任务图放行同一个道理)。"""
    if not evidence:
        empty = {"verdict": "approve", "concerns": [], "reasoning": "无支持证据, 无需审查"}
        return {"value": empty, "safety": empty, "blocked": False}
    value, safety = await asyncio.gather(
        _call_axis(_VALUE_SYSTEM_PROMPT, claim, evidence, llm_call_fn=llm_call_fn),
        _call_axis(_SAFETY_SYSTEM_PROMPT, claim, evidence, llm_call_fn=llm_call_fn),
    )
    blocked = value["verdict"] == "reject" or safety["verdict"] == "reject"
    return {"value": value, "safety": safety, "blocked": blocked}
