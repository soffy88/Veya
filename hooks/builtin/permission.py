from __future__ import annotations

from hooks.types import HookInput, HookOutput

try:
    from oskill import permission_evaluate as _pe  # type: ignore[attr-defined]
    _HAS_PERM = callable(_pe)   # guard: oskill lazy-loader may return module, not fn
    permission_evaluate = _pe if _HAS_PERM else None
except (ImportError, AttributeError):
    _HAS_PERM = False
    permission_evaluate = None

_DENY_WRITE_PERSONAS = {"plan", "research"}


async def permission_hook(inp: HookInput) -> HookOutput:
    if inp.tool_call is None:
        return HookOutput(decision="pass")

    tool_name = inp.tool_call.get("name", "")
    persona = inp.persona

    if persona in _DENY_WRITE_PERSONAS and tool_name in ("write", "edit", "bash"):
        return HookOutput(
            decision="block",
            reason=f"Persona '{persona}' 禁止写操作: {tool_name}",
        )

    if _HAS_PERM:
        result = permission_evaluate(inp.tool_call, persona=persona)  # type: ignore[name-defined]
        if result.get("allowed") is False:
            return HookOutput(decision="block", reason=result.get("reason", ""))

    return HookOutput(decision="pass")
