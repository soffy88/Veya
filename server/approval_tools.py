"""NANO-W6 model-callable tools for the approval gates.

Registered only when VEYA_APPROVAL_GATES=1 (default off): the flag keeps the
frozen tool surface unchanged for everyone who has not opted in. The
``request_handoff`` tool lets the model ask for a human step (login / captcha
/ a code) — the run pauses with a Done/Cancel resume.
"""

from __future__ import annotations

from typing import Any

from server.approvals import approval_gates_enabled


async def request_handoff_tool(reason: str) -> str:
    """Ask the user to do a manual step (login/captcha/code) before continuing."""

    from server.user_control import request_handoff

    outcome = await request_handoff(reason)
    if outcome is None:
        return "用户已完成手动步骤，可以继续。"
    return f"[手动步骤未完成] {reason}"


_TOOLS = [
    (
        "request_handoff",
        "当某个步骤只能由用户人工完成时（如登录、扫码、验证码、读取短信中的"
        "代码、到浏览器完成授权），调用此工具请求人工介入：运行会暂停，用户"
        "完成 Done 或取消 Cancel 后恢复。绝不要在模型自己能完成的步骤上调用。",
        {
            "type": "object",
            "properties": {"reason": {"type": "string"}},
            "required": ["reason"],
        },
        request_handoff_tool,
        "local_write",
    ),
]


def register_tools(registry: Any) -> int:
    """Register W6 approval tools when the gate flag is on."""

    if not approval_gates_enabled():
        return 0
    from server.tool_registry import SideEffect

    added = 0
    for name, description, parameters, func, effect in _TOOLS:
        if registry.has(name):
            continue
        registry.register(
            name,
            description,
            parameters,
            func,
            max_result_chars=2000,
            side_effect=SideEffect.LOCAL_WRITE,
            effect_capability="manual_only",
        )
        added += 1
    return added


__all__ = ["register_tools"]
