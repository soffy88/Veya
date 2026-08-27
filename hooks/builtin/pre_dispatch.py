"""H1 PreDispatch — coordinator-level pre-flight checks."""

from __future__ import annotations

from hooks.types import HookInput, HookOutput

_MAX_TEXT_LEN = 32_000


async def pre_dispatch_hook(inp: HookInput) -> HookOutput:
    """Block oversized prompts and unknown personas before dispatching squads."""
    command = inp.context.get("command", {})
    text = command.get("text", "") if isinstance(command, dict) else str(command)
    if len(text) > _MAX_TEXT_LEN:
        return HookOutput(
            decision="block",
            reason=f"Command text too long ({len(text)} chars, max {_MAX_TEXT_LEN})",
        )
    persona = inp.persona
    if persona and persona not in ("build", "plan", "research", "execute"):
        return HookOutput(decision="block", reason=f"Unknown persona: {persona!r}")
    return HookOutput(decision="pass")


async def blast_radius_gate_hook(inp: HookInput) -> HookOutput:
    """H1b blast_radius 门禁: 改代码前评估影响面 (codebase-memory-mcp 图谱)。

    条件触发: build/execute persona 且 command 含文件写意图 (write_file/edit_file/
    patch/create)。sidecar 未就绪时 pass (降级不阻断)。
    风险符号 (影响面 > 阈值) → block, 附受影响的调用方列表。
    """
    command = inp.context.get("command", {})
    text = command.get("text", "") if isinstance(command, dict) else str(command)
    if not text or inp.persona not in ("build", "execute"):
        return HookOutput(decision="pass")
    write_hints = (
        "write_file",
        "edit_file",
        "patch",
        "create_file",
        "delete_file",
        "apply_patch",
        "sed -i",
    )
    if not any(h in text for h in write_hints):
        return HookOutput(decision="pass")
    try:
        from server.codebase_memory import get_connector

        connector = get_connector()
        if not connector.ready:
            return HookOutput(decision="pass")
        # 提取命令中的符号名 (粗提取: 文件名/函数名 token)
        import re

        candidates = [
            t.strip(" '")
            for t in re.split(r"[\\/\s,;=()]", text)
            if re.match(r"^[a-zA-Z_][a-zA-Z0-9_]*\.py$", t.strip(" '"))
        ]
        if not candidates:
            return HookOutput(decision="pass")
        radius = await connector.blast_radius(candidates, depth=2)
        if radius["total_affected"] > 20:
            return HookOutput(
                decision="block",
                reason=(
                    f"blast_radius 门禁: 变更影响 {radius['total_affected']} 个符号"
                    f" (callers: {radius['callers'][:5]}…)"
                ),
            )
    except Exception:
        return HookOutput(decision="pass")  # 降级: 图谱不可用不阻断
    return HookOutput(decision="pass")
