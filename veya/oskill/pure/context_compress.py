"""3O-PURE — context_compress: 滑动窗口 / 关键信息抽取。

从现有主循环逻辑纯函数化（master_agent 的历史滑窗）：
- ``sliding_window``: 保首尾的定长窗口（等价旧 ``messages[:] = [messages[0]] + messages[-n+1:]``）；
- ``truncate_to_token_budget``: 按 token 估算预算从前往后裁剪（系统消息钉住）；
- ``estimate_tokens``: 确定性启发式 token 估算（无随机、无 I/O）。
"""

from __future__ import annotations

from collections.abc import Callable

# 确定性启发式: 中文≈1 token/字符, 英文≈4 字符/token
_CJK_RANGES = (
    (0x4E00, 0x9FFF),   # CJK 统一表意文字
    (0x3400, 0x4DBF),   # 扩展 A
    (0xF900, 0xFAFF),   # 兼容表意文字
    (0x3040, 0x30FF),   # 日文假名
    (0xAC00, 0xD7AF),   # 韩文
)


def _is_cjk(ch: str) -> bool:
    code = ord(ch)
    return any(lo <= code <= hi for lo, hi in _CJK_RANGES)


def estimate_tokens(text: str) -> int:
    """确定性 token 估算：CJK 字符按 1 token，其余按 4 字符 1 token。"""
    if not text:
        return 0
    cjk = sum(1 for ch in text if _is_cjk(ch))
    other = len(text) - cjk
    return max(1, cjk + (other + 3) // 4)


def sliding_window(messages: list, *, max_messages: int, system_pinned: bool = True) -> list:
    """保首尾滑窗：超窗时钉住系统消息（若有），其余取最近 ``max_messages-1`` 条。"""
    if max_messages < 1:
        raise ValueError("max_messages 必须 >= 1")
    if len(messages) <= max_messages:
        return list(messages)
    head: list = []
    if system_pinned and messages and isinstance(messages[0], dict) and messages[0].get("role") == "system":
        head = [messages[0]]
    tail = messages[-(max_messages - len(head)) :]
    return head + tail


def truncate_to_token_budget(
    messages: list,
    *,
    max_tokens: int,
    estimate_fn: Callable[[str], int] | None = None,
) -> list:
    """按 token 预算裁剪：系统消息钉住，其余按序从旧到新丢弃。

    ``estimate_fn`` 注入估算函数（默认 estimate_tokens）；返回的列表
    总 token 不超过预算（单条超预算消息保留但截断 content）。
    """
    est = estimate_fn or estimate_tokens
    if max_tokens < 1:
        raise ValueError("max_tokens 必须 >= 1")

    system: list = []
    rest: list = []
    for msg in messages:
        if isinstance(msg, dict) and msg.get("role") == "system":
            system.append(msg)
        else:
            rest.append(msg)

    budget = max_tokens
    kept_system: list = []
    for msg in system:
        cost = _message_tokens(msg, est)
        if cost <= budget:
            kept_system.append(msg)
            budget -= cost
    # 超过一半预算用于系统消息时, 只保留最近一条系统消息
    if kept_system and sum(_message_tokens(m, est) for m in kept_system) > max_tokens // 2:
        kept_system = [kept_system[-1]]
        budget = max_tokens - _message_tokens(kept_system[-1], est)

    kept: list = []
    for msg in reversed(rest):
        cost = _message_tokens(msg, est)
        if cost <= budget:
            kept.append(msg)
            budget -= cost
        elif kept:
            break
    kept.reverse()
    # 最后一条超预算消息: 截断 content 保住信号
    if rest and not kept:
        last = dict(rest[-1])
        content = last.get("content")
        if isinstance(content, str):
            last["content"] = content[: max(0, budget * 4)]
        kept = [last]
    return kept_system + kept


def _message_tokens(msg: dict, est: Callable[[str], int]) -> int:
    content = msg.get("content")
    cost = est(content) if isinstance(content, str) else 0
    tcs = msg.get("tool_calls")
    if isinstance(tcs, list):
        for tc in tcs:
            if isinstance(tc, dict):
                cost += est(str(tc.get("name", ""))) + est(str(tc.get("arguments", "")))
    return cost


def extract_key_info(text: str, *, max_chars: int = 200) -> str:
    """确定性关键信息抽取：头部截断 + 省略号（保持纯函数，无语义模型）。

    阶段 2 先提供确定性基线；后续可注入摘要器（仍须保持输入→输出纯函数）。
    """
    if not text:
        return ""
    stripped = text.strip()
    if len(stripped) <= max_chars:
        return stripped
    return stripped[: max(0, max_chars - 1)] + "…"


__all__ = [
    "estimate_tokens",
    "extract_key_info",
    "sliding_window",
    "truncate_to_token_budget",
]
