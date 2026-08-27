"""3O-PURE — evaluate_stop_condition: 完成/最大轮次/致命错误判断。

从现有主循环逻辑纯函数化（master_agent 的停止分支 + 空回复兜底检测）：
- 致命错误 → 立即停止（kind=fatal_error）；
- 达到最大轮次 → 停止（kind=max_rounds）；
- 无 tool_calls → 任务完成；内容为空/None/null 视为 invalid_response；
- 否则 → 继续（kind=continue）。

纯函数：所有输入显式传参，无 I/O、无全局、无随机。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# 模型疲劳空回复集合（与现有主循环一致）。
# 2026-08-16 修复: 移除 "ok"/"done"/"完成"/"已完成" — 这些是有内容的正常回复
# （如用户问「可以吗」模型答 ok），误判为疲劳会让正常对话报
# 「循环停止 (invalid_response)」错误（HTTP 全路径实测: 模型回 'ok' 被误杀）。
# 只保留真正「无内容」的标记: 空串与 null/none 变体。
_INVALID_CONTENTS: frozenset[str] = frozenset({"", "none", "null", "nil", "n/a", "无", "空"})


@dataclass(frozen=True)
class StopDecision:
    """停止决策。stop=True 时 reason 给出人类可读说明。"""

    stop: bool
    reason: str = ""
    kind: str = "continue"  # continue | completed | max_rounds | fatal_error | invalid_response


def is_invalid_response(content: Any) -> bool:
    """内容是否为空回复/模型疲劳标记（纯函数）。"""
    if content is None:
        return True
    if not isinstance(content, str):
        return False
    return content.strip().lower() in _INVALID_CONTENTS


def evaluate_stop_condition(
    *,
    round_count: int,
    max_rounds: int,
    tool_calls: list | None = None,
    last_content: Any = None,
    last_error: str | None = None,
    fatal_error: str | None = None,
    completed_content: Any = None,
) -> StopDecision:
    """主循环停止判断。

    参数:
        round_count: 已执行轮次（0 起）
        max_rounds: 轮次上限（<=0 视为无上限）
        tool_calls: 本轮 LLM 输出中的 tool_calls（空列表 = 直接回答）
        last_content: 本轮 assistant 内容
        last_error: 本轮工具执行错误（非致命）
        fatal_error: 致命错误（LLM 调用失败/基础设施故障）
        completed_content: 显式完成内容（若有则优先视为完成）
    """
    if fatal_error:
        return StopDecision(stop=True, reason=f"致命错误: {fatal_error}", kind="fatal_error")

    if completed_content is not None:
        return StopDecision(
            stop=True,
            reason="模型显式输出完成标记",
            kind="completed",
        )

    if max_rounds > 0 and round_count >= max_rounds:
        return StopDecision(
            stop=True,
            reason=f"达到最大轮次 {max_rounds}",
            kind="max_rounds",
        )

    has_tools = isinstance(tool_calls, list) and len(tool_calls) > 0
    if not has_tools:
        # 模型直接回答: 内容合法 → completed; 空/疲劳回复 → invalid_response
        if is_invalid_response(last_content):
            reason = (
                "模型返回无效响应 (空/疲劳标记)"
                if last_error is None
                else f"模型返回无效响应; 最近工具错误: {last_error}"
            )
            return StopDecision(stop=True, reason=reason, kind="invalid_response")
        return StopDecision(stop=True, reason="模型直接回答, 任务完成", kind="completed")

    # 有 tool_calls → 继续执行工具
    return StopDecision(stop=False, reason="继续执行工具", kind="continue")


__all__ = [
    "StopDecision",
    "evaluate_stop_condition",
    "is_invalid_response",
]
