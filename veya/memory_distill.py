"""veya/memory_distill.py — 对话蒸馏 (P4).

把一段对话消息蒸馏成结构化记忆: 持久事实 / 偏好规则 / 一句话背景摘要。
用注入的 llm_fn 跑 (测试注入 stub; 线上走主脑同一模型)。解析容错: 允许
markdown 围栏, 解析失败 → 空结果 (绝不抛)。

设计: 蒸馏在**对话主链路之外**异步跑 (不阻塞回答), 见 coordinator_master
的 _schedule_distill。质量 (抽取得准不准) 依赖真实模型, 须生产环境验证。
"""

from __future__ import annotations

import json
from typing import Any

_DISTILL_SYSTEM = (
    "你是一个记忆蒸馏器。读一段用户与助手的对话, 抽取值得**长期记住**的信息。"
    "只保留跨会话仍成立的通用事实/偏好, 不要一次性指令或寒暄。"
    "严格输出 JSON (不要多余文字, 可用 ```json 围栏):\n"
    '{"facts": ["关于用户/项目的持久事实", ...],'
    ' "preferences": ["用户的偏好/工作方式/纠正", ...],'
    ' "summary": "一句话概括本次对话的核心 (<=40字)"}'
)


def _extract_json(text: str) -> dict[str, Any]:
    if not text:
        return {}
    s = text.strip()
    if "```" in s:  # 去 markdown 围栏
        parts = s.split("```")
        for p in parts:
            p = p.strip()
            if p.startswith("json"):
                p = p[4:].strip()
            if p.startswith("{"):
                s = p
                break
    start, end = s.find("{"), s.rfind("}")
    if start == -1 or end == -1 or end < start:
        return {}
    try:
        data = json.loads(s[start : end + 1])
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, TypeError):
        return {}


def _render_conversation(messages: list[dict[str, Any]], max_chars: int = 6000) -> str:
    lines: list[str] = []
    for m in messages:
        role = m.get("role", "")
        if role == "system":
            continue
        content = m.get("content")
        if not content:
            continue
        lines.append(f"{role}: {content}")
    blob = "\n".join(lines)
    return blob[-max_chars:] if len(blob) > max_chars else blob


async def distill(messages: list[dict[str, Any]], llm_fn: Any) -> dict[str, Any]:
    """→ {"facts": [...], "preferences": [...], "summary": str}。失败返回空结构。"""
    convo = _render_conversation(messages)
    if not convo.strip():
        return {"facts": [], "preferences": [], "summary": ""}
    prompt_messages = [
        {"role": "system", "content": _DISTILL_SYSTEM},
        {"role": "user", "content": f"对话:\n{convo}\n\n按上面格式输出 JSON。"},
    ]
    try:
        resp = await llm_fn(prompt_messages)
    except Exception:  # 蒸馏失败绝不影响主对话
        return {"facts": [], "preferences": [], "summary": ""}
    content = ""
    if isinstance(resp, dict):
        choices = resp.get("choices") or []
        if choices:
            content = (choices[0].get("message") or {}).get("content") or ""
        content = content or resp.get("final_answer") or resp.get("content") or ""
    elif isinstance(resp, str):
        content = resp
    data = _extract_json(content)
    return {
        "facts": [str(x).strip() for x in (data.get("facts") or []) if str(x).strip()],
        "preferences": [str(x).strip() for x in (data.get("preferences") or []) if str(x).strip()],
        "summary": str(data.get("summary") or "").strip(),
    }
