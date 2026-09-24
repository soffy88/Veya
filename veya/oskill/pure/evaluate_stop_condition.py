"""3O-PURE — evaluate_stop_condition: 语义化完成/阻塞/错误/无进展判断。

从现有主循环逻辑纯函数化（master_agent 的停止分支 + 空回复兜底检测）：
- 致命错误 → 立即停止（kind=fatal_error）；
- 语义完成 → 停止（kind=completed）；
- 被阻塞 → 停止（kind=blocked）；
- 用户取消 → 停止（kind=cancelled）；
- 安全/资源耗尽 → 停止（kind=safety_resource_exhausted）；
- 无进展检测 → 停止（kind=no_progress_detected）；
- 无效响应 → 停止（kind=invalid_response）；
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
    kind: str = "continue"  # continue | completed | blocked | cancelled | fatal_error | safety_resource_exhausted | no_progress_detected | invalid_response


def is_invalid_response(content: Any) -> bool:
    """内容是否为空回复/模型疲劳标记（纯函数）。"""
    if content is None:
        return True
    if not isinstance(content, str):
        return False
    return content.strip().lower() in _INVALID_CONTENTS


def evaluate_stop_condition(
    *,
    round_count: int = 0,
    tool_calls: list | None = None,
    last_content: Any = None,
    last_error: str | None = None,
    fatal_error: str | None = None,
    completed_content: Any = None,
    blocked_reason: str | None = None,
    cancelled: bool = False,
    safety_resource_exhausted: str | None = None,
    no_progress_detected: str | None = None,
    **_legacy_kwargs: Any,
) -> StopDecision:
    """主循环停止判断（语义化）。

    参数:
        round_count: 已执行轮次（0 起，仅作 telemetry）
        tool_calls: 本轮 LLM 输出中的 tool_calls（空列表 = 直接回答）
        last_content: 本轮 assistant 内容
        last_error: 本轮工具执行错误（非致命）
        fatal_error: 致命错误（LLM 调用失败/基础设施故障）
        completed_content: 显式完成内容（若有则优先视为完成）
        blocked_reason: 被阻塞原因（需用户输入/权限/credential/审批）
        cancelled: 用户显式取消
        safety_resource_exhausted: 安全/资源耗尽原因（wall-clock/预算/配额）
        no_progress_detected: 无进展检测原因（循环/重复无新证据）
    """
    if fatal_error:
        return StopDecision(stop=True, reason=f"致命错误: {fatal_error}", kind="fatal_error")

    if cancelled:
        return StopDecision(stop=True, reason="用户取消", kind="cancelled")

    if safety_resource_exhausted:
        return StopDecision(
            stop=True,
            reason=f"安全/资源限制: {safety_resource_exhausted}",
            kind="safety_resource_exhausted",
        )

    if no_progress_detected:
        return StopDecision(
            stop=True,
            reason=f"无进展检测: {no_progress_detected}",
            kind="no_progress_detected",
        )

    if blocked_reason:
        return StopDecision(stop=True, reason=f"被阻塞: {blocked_reason}", kind="blocked")

    if completed_content is not None:
        return StopDecision(
            stop=True,
            reason="模型显式输出完成标记",
            kind="completed",
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
