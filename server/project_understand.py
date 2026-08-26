"""project_understand — Understand 门禁 (docs/PROJECT_AGENT.md §7, 2026-08-16)。

在 project_ask 的派工决策前插入一次轻量判定: 能确信实现方案与验收标准就
decision=act（沿用既有 builtin/hicode/dsh 执行腿）；有歧义就 decision=ask，
只追问、不产生任何业务副作用（不建 .veya-project/ runs/ 之外的文件、不派工）。

单一 LLM 调用 + 硬约束校验；解析失败或校验不自洽一律安全降级为 ask ——
宁可多问一句，也不放行一个不确定的 act。测试通过 `_llm` 注入桩，不依赖真模型。
"""

from __future__ import annotations

import dataclasses
import json
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal

_CONFIDENCE_MIN = float(os.environ.get("PROJECT_UNDERSTAND_CONFIDENCE_MIN", "0.75"))
_MAX_QUESTIONS = int(os.environ.get("PROJECT_UNDERSTAND_MAX_QUESTIONS", "3"))
_MEMORY_CHARS = int(os.environ.get("PROJECT_UNDERSTAND_MEMORY_CHARS", "6000"))

_DEFAULT_QUESTION = "请用一两句话说明目标产物与验收标准。"

# 同步 Llm 注入是 openrsi/reasoning_bank 的既有约定 (server/unified_pipeline.py);
# understand() 本身在 project_ask 的 async 路径里跑, 直接注入 async (system, user) -> str。
UnderstandLlm = Callable[[str, str], Awaitable[str]]


@dataclass
class UnderstandResult:
    decision: Literal["act", "ask"]
    confidence: float
    interpretation: str
    assumptions: list[str] = field(default_factory=list)
    questions: list[str] = field(default_factory=list)
    risk_flags: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)


_SYSTEM_PROMPT = (
    "你是需求门禁, 只输出一个 JSON 对象, 不输出任何其它文字, 不执行任何工具/命令。\n"
    "根据用户请求与项目记忆判断: 能确信实现方案与验收标准就 decision=act; "
    "存在多解读、无验收标准、范围不清、破坏性操作、或与项目记忆明确冲突就 decision=ask。\n"
    'JSON 字段: decision("act"|"ask"), confidence(0.0-1.0), interpretation(string, '
    "一句话复述将要做什么), assumptions(string[]), questions(string[]), risk_flags(string[]), "
    "reasons(string[], 简短机器可读原因)。\n"
    'decision="ask" 时 questions 必须 1-3 条, 每条只问一件事, 优先给选项；'
    'decision="act" 时 questions 必须是空数组, interpretation 不能为空。'
)


def _clarification_chain_block(chain: list[dict[str, Any]]) -> str:
    if not chain:
        return ""
    parts = ["## Prior clarification round"]
    for turn in chain:
        parts.append(f"- prior request: {turn.get('request', '')}")
        parts.append(f"- prior interpretation: {turn.get('interpretation', '')}")
        for q in turn.get("questions") or []:
            parts.append(f"  - Q: {q}")
    return "\n".join(parts)


def _build_user_message(request: str, memory: str, chain: list[dict[str, Any]]) -> str:
    parts = [f"## Project memory (truncated)\n{memory[:_MEMORY_CHARS]}"]
    chain_block = _clarification_chain_block(chain)
    if chain_block:
        parts.append(chain_block)
    parts.append(f"## User request (may be an answer to the prior questions above)\n{request}")
    return "\n\n".join(parts)


def _str_list(data: dict[str, Any], key: str) -> list[str]:
    v = data.get(key)
    if not isinstance(v, list):
        return []
    return [str(x) for x in v if str(x).strip()]


def _parse(raw: str) -> UnderstandResult | None:
    """宽松解析: 允许模型在 JSON 前后夹带说明文字/围栏。解析失败返回 None。"""
    data: Any = None
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        start, end = raw.find("{"), raw.rfind("}")
        if start == -1 or end == -1 or end <= start:
            return None
        try:
            data = json.loads(raw[start : end + 1])
        except json.JSONDecodeError:
            return None
    if not isinstance(data, dict):
        return None
    decision = data.get("decision")
    if decision not in ("act", "ask"):
        return None
    try:
        confidence = float(data.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    return UnderstandResult(
        decision=decision,
        confidence=confidence,
        interpretation=str(data.get("interpretation") or ""),
        assumptions=_str_list(data, "assumptions"),
        questions=_str_list(data, "questions"),
        risk_flags=_str_list(data, "risk_flags"),
        reasons=_str_list(data, "reasons"),
    )


def _force_ask(reason: str, interpretation: str = "", confidence: float = 0.0) -> UnderstandResult:
    return UnderstandResult(
        decision="ask",
        confidence=confidence,
        interpretation=interpretation,
        assumptions=[],
        questions=[_DEFAULT_QUESTION],
        risk_flags=[],
        reasons=[reason],
    )


def validate_or_force_ask(u: UnderstandResult) -> UnderstandResult:
    """硬约束校验 (PROJECT_AGENT.md §7.2): 任何不自洽都安全降级为 ask。"""
    if u.decision == "act":
        if u.questions:
            return _force_ask("act 却带 questions, 判定不自洽", u.interpretation, u.confidence)
        if not u.interpretation.strip():
            return _force_ask("act 但 interpretation 为空", u.interpretation, u.confidence)
        if u.confidence < _CONFIDENCE_MIN:
            return _force_ask(
                f"confidence {u.confidence} < {_CONFIDENCE_MIN}", u.interpretation, u.confidence
            )
        return u

    questions = u.questions[:_MAX_QUESTIONS] or [_DEFAULT_QUESTION]
    if questions != u.questions:
        u = dataclasses.replace(u, questions=questions)
    return u


def eager_act_result(request: str) -> UnderstandResult:
    """mode=act_eager: 跳过 LLM 判定, 直接放行执行 (调用方需明确承担未澄清的风险)。"""
    return UnderstandResult(
        decision="act",
        confidence=1.0,
        interpretation=request[:500],
        assumptions=["act_eager: 未做澄清"],
        questions=[],
        risk_flags=[],
        reasons=["mode=act_eager"],
    )


def force_confirm_ask(u: UnderstandResult) -> UnderstandResult:
    """mode=ask_only: 即便判定 act, 也强制转成一次确认问, 永不执行。"""
    if u.decision == "ask":
        return u
    return UnderstandResult(
        decision="ask",
        confidence=u.confidence,
        interpretation=u.interpretation,
        assumptions=u.assumptions,
        questions=[f"我理解成: {u.interpretation}。这样对吗?如需调整请说明。"],
        risk_flags=u.risk_flags,
        reasons=["mode=ask_only: 强制确认"],
    )


async def _default_llm(system: str, user: str) -> str:
    from veya.llm import llm_call

    # 显式 provider=veya1.2 → GMI MiniMax M3 + OpenRouter 兜底。
    # 不能用无参 llm_call: 默认 provider 是 dashscope, 本机无其 key 时会
    # 返回 shim 文本 ("LLM provider not configured"), 被 _parse 判为解析失败
    # → 永远安全降级 ask (U5 真机 smoke 2026-08-16 抓到)。
    resp = await llm_call(
        [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        provider="veya1.2",
    )
    return ((resp.get("choices") or [{}])[0].get("message") or {}).get("content") or ""


async def understand(
    request: str,
    memory: str,
    chain: list[dict[str, Any]] | None = None,
    *,
    _llm: UnderstandLlm | None = None,
) -> UnderstandResult:
    """跑一次判定并做硬约束校验; 解析失败/LLM 调用异常一律安全降级为 ask。"""
    chain = chain or []
    user_msg = _build_user_message(request, memory, chain)
    llm = _llm or _default_llm

    try:
        raw = await llm(_SYSTEM_PROMPT, user_msg)
    except Exception as exc:  # noqa: BLE001 — 判定失败也要收敛为 ask, 不裸露异常给调用方
        return _force_ask(f"understand LLM 调用异常: {exc}")

    parsed = _parse(raw)
    if parsed is None:
        return _force_ask("LLM 输出解析失败, 安全降级")
    return validate_or_force_ask(parsed)


__all__ = [
    "UnderstandResult",
    "understand",
    "validate_or_force_ask",
    "eager_act_result",
    "force_confirm_ask",
]
