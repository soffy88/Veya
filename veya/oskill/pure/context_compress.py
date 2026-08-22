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
    (0x4E00, 0x9FFF),  # CJK 统一表意文字
    (0x3400, 0x4DBF),  # 扩展 A
    (0xF900, 0xFAFF),  # 兼容表意文字
    (0x3040, 0x30FF),  # 日文假名
    (0xAC00, 0xD7AF),  # 韩文
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
    if (
        system_pinned
        and messages
        and isinstance(messages[0], dict)
        and messages[0].get("role") == "system"
    ):
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


def estimate_messages_tokens(
    messages: list, *, estimate_fn: Callable[[str], int] | None = None
) -> int:
    """整批消息的 token 估算总量（`_message_tokens` 对外公开版本）。"""
    est = estimate_fn or estimate_tokens
    return sum(_message_tokens(m, est) for m in messages if isinstance(m, dict))


def should_compact(
    messages: list,
    *,
    max_tokens: int,
    trigger_ratio: float = 0.7,
    estimate_fn: Callable[[str], int] | None = None,
) -> bool:
    """是否该做结构化压缩：估算总 token 数达到预算的 ``trigger_ratio`` 比例。

    纯判断，不读环境变量（解析职责留给调用方）。``max_tokens<=0`` 或
    ``trigger_ratio<=0`` 视为关闭，恒 False。
    """
    if max_tokens <= 0 or trigger_ratio <= 0:
        return False
    total = estimate_messages_tokens(messages, estimate_fn=estimate_fn)
    return total >= max_tokens * trigger_ratio


def split_compaction_window(messages: list, *, keep_tail_messages: int) -> tuple[list, list, list]:
    """把 messages 切成 (head, to_compact, tail)，供 LLM 摘要 + 合并回填。

    - ``head``：若首条是 system 消息则钉住为 head，其余进 body。
    - body 先切成"原子单元"：一条带 ``tool_calls`` 的 assistant 消息，必须和
      紧随其后、``tool_call_id`` 匹配的全部 ``role=="tool"`` 结果消息绑成同一个
      不可拆单元——尾部窗口切分绝不能把一次工具调用和它的结果拆到两侧，否则
      合并回填后的消息序列会出现协议非法的孤儿 tool 消息。``tool_call_id`` 不
      匹配的孤儿 tool 消息保守地单独成一个单元，不强行粘连。
    - 从尾部按整单元收，直到累计条数达到 ``keep_tail_messages``；可能超出目标
      值（一次工具调用组可能一次带多条结果），这是预期行为，不是 bug。
    - ``to_compact``/``tail`` 是同一批原子单元的互补切片，退化场景（body 为
      空、``keep_tail_messages<=0``、单个原子单元本身超过尾部目标）时
      ``to_compact`` 可能为空，调用方应据此跳过本轮压缩。
    """
    if messages and isinstance(messages[0], dict) and messages[0].get("role") == "system":
        head = [messages[0]]
        body = messages[1:]
    else:
        head = []
        body = list(messages)

    units: list[list[dict]] = []
    i = 0
    n = len(body)
    while i < n:
        msg = body[i]
        tool_calls = msg.get("tool_calls") if isinstance(msg, dict) else None
        if (
            isinstance(msg, dict)
            and msg.get("role") == "assistant"
            and isinstance(tool_calls, list)
            and tool_calls
        ):
            ids = {tc.get("id") for tc in tool_calls if isinstance(tc, dict) and tc.get("id")}
            j = i + 1
            consumed: list[dict] = []
            while j < n:
                nxt = body[j]
                if not (isinstance(nxt, dict) and nxt.get("role") == "tool"):
                    break
                if ids and nxt.get("tool_call_id") not in ids:
                    break
                consumed.append(nxt)
                j += 1
                if ids and len(consumed) >= len(ids):
                    break
            units.append([msg, *consumed])
            i = j
        else:
            units.append([msg])
            i += 1

    if keep_tail_messages <= 0:
        return head, _flatten(units), []

    tail_units: list[list[dict]] = []
    tail_len = 0
    idx = len(units) - 1
    while idx >= 0 and tail_len < keep_tail_messages:
        tail_units.insert(0, units[idx])
        tail_len += len(units[idx])
        idx -= 1
    to_compact_units = units[: idx + 1]
    return head, _flatten(to_compact_units), _flatten(tail_units)


def _flatten(units: list[list[dict]]) -> list[dict]:
    out: list[dict] = []
    for u in units:
        out.extend(u)
    return out


def render_messages_for_summary(messages: list, *, max_chars: int = 8000) -> str:
    """把待压缩片段渲染成摘要 LLM 的输入文本。

    与 ``memory_distill._render_conversation`` 的关键差异：
    - 带 ``tool_calls`` 的 assistant 消息即使 ``content`` 为空也要渲染（展示
      调用了什么工具/传了什么参数），否则摘要读起来会断片。
    - ``role=="tool"`` 结果逐条截断到 500 字符，防止单条巨型工具输出在最终
      整体 ``max_chars`` 尾部截断时独占预算、把真正的对话线索挤出摘要输入。
    """
    lines: list[str] = []
    for m in messages:
        if not isinstance(m, dict):
            continue
        role = m.get("role", "")
        if role == "system":
            continue
        if role == "assistant":
            tool_calls = m.get("tool_calls")
            content = m.get("content")
            if isinstance(tool_calls, list) and tool_calls:
                calls = []
                for tc in tool_calls:
                    if not isinstance(tc, dict):
                        continue
                    fn = tc.get("function") if isinstance(tc.get("function"), dict) else tc
                    name = fn.get("name", "") if isinstance(fn, dict) else ""
                    args = str(fn.get("arguments", "") if isinstance(fn, dict) else "")[:200]
                    calls.append(f"{name}({args})")
                lines.append(f"assistant(调用工具): {'; '.join(calls)}")
                if content:
                    lines.append(f"assistant: {content}")
                continue
            if content:
                lines.append(f"assistant: {content}")
            continue
        if role == "tool":
            content = str(m.get("content") or "")[:500]
            lines.append(f"tool[{m.get('tool_call_id', '')}]: {content}")
            continue
        content = m.get("content")
        if content:
            lines.append(f"{role}: {content}")
    blob = "\n".join(lines)
    return blob[-max_chars:] if len(blob) > max_chars else blob


_COMPACT_SUMMARY_PREFIX = "[COMPACTION SUMMARY — earlier turns condensed]"


def build_compacted_messages(
    head: list,
    summary_text: str,
    tail: list,
    *,
    summary_role: str = "assistant",
    summary_prefix: str = _COMPACT_SUMMARY_PREFIX,
) -> list:
    """合并回填：``[*head, summary_msg, *tail]``。

    ``summary_role`` 必须是 ``"assistant"``，不能是 ``"system"``——
    ``coordinator_master._persist_history`` 会把所有 ``role=="system"`` 消息
    剥离后再落盘，压缩摘要若用 system role，进程重启后这条摘要本身就会消失，
    恰恰摧毁了压缩要解决的"长会话跨重启存活"目标。
    """
    summary_msg = {"role": summary_role, "content": f"{summary_prefix}\n{summary_text}"}
    return [*head, summary_msg, *tail]


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
    "build_compacted_messages",
    "estimate_messages_tokens",
    "estimate_tokens",
    "extract_key_info",
    "render_messages_for_summary",
    "should_compact",
    "sliding_window",
    "split_compaction_window",
    "truncate_to_token_budget",
]
