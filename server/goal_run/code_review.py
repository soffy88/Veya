"""server.goal_run.code_review — 双轴独立代码审查(mattpocock/skills code-review 内化)。

两条轴从不合并/不重新排名: **Standards**(这段代码写得对不对——按仓库自己的
约定)和 **Spec**(这段代码做的是不是被要求的那件事)。各自一次独立 LLM 调用,
互不看对方的推理(两个子agent互相看不到彼此), 报告以两个独立区块呈现, 各给
一个"本轴最严重问题", 从不给一个混合总分——一处改动完全可能守规矩但做错了
方向, 也可能做对了方向但踩了仓库的约定, 混成一个分数会让过的那半掩盖没过
的那半。

Standards 轴的地板是《重构》第三章 12 个经典坏味道(Mysterious Name/
Duplicated Code/Feature Envy/Data Clumps/Primitive Obsession/Repeated
Switches/Shotgun Surgery/Divergent Change/Speculative Generality/Message
Chains/Middle Man/Refused Bequest), 不是审查的全部——仓库自己的文档
(CLAUDE.md 等)永远优先。

结论只是建议(advisory), 不自动拦任务完成——LLM 判断是假设不是证据(参照
mattpocock/skills 原文的告诫), goal_run 的 acceptance 验收才是真正的准入门。
diff 为空/LLM 不可用都优雅退化为空报告, 不拖垮任务调度。
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Awaitable, Callable
from typing import Any

from veya.llm import llm_call

_STUB_MARKER = "LLM provider not configured"

_TWELVE_SMELLS = (
    "Mysterious Name",
    "Duplicated Code",
    "Feature Envy",
    "Data Clumps",
    "Primitive Obsession",
    "Repeated Switches",
    "Shotgun Surgery",
    "Divergent Change",
    "Speculative Generality",
    "Message Chains",
    "Middle Man",
    "Refused Bequest",
)

_STANDARDS_SYSTEM_PROMPT = f"""You review a code diff against how THIS repository \
writes code. Primary source: the repository's own documented standards (given \
below, e.g. CLAUDE.md) — they always override the baseline. If no repo \
standards are given, or on top of them, use this floor: the twelve Fowler \
code smells from Refactoring ch.3: {", ".join(_TWELVE_SMELLS)}. Each smell is \
a labelled heuristic ("possible Feature Envy"), never a hard violation — \
state what it is and how to fix it, not just a complaint. Skip anything a \
linter would already catch mechanically (formatting, unused imports).

Respond with ONLY a JSON object, no other text:
{{"findings": [{{"rule": "...", "detail": "...", "hunk": "..."}}], "worst": "one-sentence worst issue, or null if none"}}"""

_SPEC_SYSTEM_PROMPT = """You review a code diff against what it was asked to do. \
You are given the task instruction and its acceptance criteria. Report \
missing or partial requirements, scope creep (work outside what was asked), \
and requirements implemented in a way that doesn't actually satisfy them. \
Every finding must quote the line of the instruction/acceptance it concerns. \
If the diff looks empty or unrelated to the task, say so plainly.

Respond with ONLY a JSON object, no other text:
{"findings": [{"requirement": "...", "detail": "..."}], "worst": "one-sentence worst issue, or null if none"}"""


def _parse_axis(raw: str) -> dict[str, Any]:
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        return {"findings": [], "worst": None, "unscanned_reason": f"未能解析模型输出: {raw[:300]}"}
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return {
            "findings": [],
            "worst": None,
            "unscanned_reason": f"模型输出非合法 JSON: {raw[:300]}",
        }
    return {
        "findings": list(data.get("findings") or []),
        "worst": data.get("worst") or None,
    }


async def _call_axis(
    system_prompt: str, user_content: str, *, llm_call_fn: Callable[..., Awaitable[dict]]
) -> dict[str, Any]:
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content},
    ]
    try:
        resp = await llm_call_fn(messages)
    except Exception as exc:
        return {"findings": [], "worst": None, "unscanned_reason": f"{type(exc).__name__}: {exc}"}
    content = ""
    if isinstance(resp, dict):
        choices = resp.get("choices") or []
        if choices:
            content = (choices[0].get("message") or {}).get("content") or ""
    if not content or _STUB_MARKER in content:
        return {
            "findings": [],
            "worst": None,
            "unscanned_reason": "LLM 未配置(stub 回落), 未能审查",
        }
    return _parse_axis(content)


async def review_standards(
    diff_text: str,
    *,
    standards_doc: str = "",
    llm_call_fn: Callable[..., Awaitable[dict]] = llm_call,
) -> dict[str, Any]:
    if not diff_text.strip():
        return {"findings": [], "worst": None, "unscanned_reason": "diff 为空"}
    doc_block = (
        f"仓库文档标准:\n{standards_doc}\n\n"
        if standards_doc.strip()
        else "(仓库未提供文档标准, 只用12坏味道地板)\n\n"
    )
    user_content = f"{doc_block}Diff:\n{diff_text}"
    return await _call_axis(_STANDARDS_SYSTEM_PROMPT, user_content, llm_call_fn=llm_call_fn)


async def review_spec(
    diff_text: str,
    *,
    task_instruction: str,
    acceptance: list[str],
    llm_call_fn: Callable[..., Awaitable[dict]] = llm_call,
) -> dict[str, Any]:
    if not diff_text.strip():
        return {"findings": [], "worst": None, "unscanned_reason": "diff 为空"}
    accept_block = "\n".join(f"- {a}" for a in acceptance) or "(无显式验收条件)"
    user_content = (
        f"任务指令:\n{task_instruction}\n\n验收条件:\n{accept_block}\n\nDiff:\n{diff_text}"
    )
    return await _call_axis(_SPEC_SYSTEM_PROMPT, user_content, llm_call_fn=llm_call_fn)


async def dual_axis_review(
    *,
    diff_text: str,
    task_instruction: str,
    acceptance: list[str],
    standards_doc: str = "",
    llm_call_fn: Callable[..., Awaitable[dict]] = llm_call,
) -> dict[str, Any]:
    """并行跑两条独立轴, 从不合并结论。空 diff/LLM 失败都优雅退化, advisory only。"""
    standards, spec = await asyncio.gather(
        review_standards(diff_text, standards_doc=standards_doc, llm_call_fn=llm_call_fn),
        review_spec(
            diff_text,
            task_instruction=task_instruction,
            acceptance=acceptance,
            llm_call_fn=llm_call_fn,
        ),
    )
    return {"standards": standards, "spec": spec}
