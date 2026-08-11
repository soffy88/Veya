"""server/tool_guard_policies — 默认工具守卫策略 (确定性笼子的具体闸门).

把既有「咨询式」安全闸 (``state_kernel.terminal_gate_check``, 原本只是主脑可选
调用的工具) 接成 ``tool_guard`` 的**强制中间件**候选。

安全灰度 (功能至上):
- 缺省 **observe 模式** (``VEYA_TOOL_GATE_ENFORCE`` != "1"): 命中只落 ``observed``
  轨迹, 不拦截任何工具 —— 先采样「哪些工具会被判为 terminal」, 零行为变化;
- 置 ``VEYA_TOOL_GATE_ENFORCE=1`` 后翻 **enforce**: terminal/不可逆动作被拦截,
  回喂主脑「需人工审批」由模型改走 ``system_secure_exec`` / 询问用户。
"""

from __future__ import annotations

import json
import os

from server.tool_guard import ToolGuard, global_tool_guard

_TERMINAL_POLICY = "terminal_action_gate"

# 拼入 action 分类的承载动作语义的参数键 (命令/操作/路径类)。
_ACTION_ARG_KEYS = ("command", "cmd", "action", "operation", "op", "path", "target", "url")


async def terminal_action_policy(name: str, kwargs: dict, source: str) -> str | None:
    """命中 terminal/不可逆动作 → 返回拒绝原因; 否则 None (放行)。

    以「工具名 + 关键字符串参数」拼成 action, 交 ``terminal_gate_check`` 关键词
    分类 (deploy/publish/delete/drop/rm ...)。分类失败一律 fail-open (返回 None)。
    """
    from server.state_kernel import terminal_gate_check

    action = name
    for key in _ACTION_ARG_KEYS:
        val = kwargs.get(key)
        if isinstance(val, str) and val:
            action += " " + val
    try:
        verdict = json.loads(await terminal_gate_check(action))
    except Exception:
        return None
    if verdict.get("requires_approval"):
        return str(verdict.get("reason") or "terminal/irreversible action requires human approval")
    return None


def install_default_tool_policies(guard: ToolGuard | None = None) -> None:
    """幂等安装默认守卫策略。缺省 observe 模式 (VEYA_TOOL_GATE_ENFORCE=1 翻 enforce)。"""
    guard = guard or global_tool_guard
    if guard.has_policy(_TERMINAL_POLICY):
        return
    enforce = os.environ.get("VEYA_TOOL_GATE_ENFORCE", "0") == "1"
    guard.register_policy(_TERMINAL_POLICY, terminal_action_policy, enforce=enforce)
