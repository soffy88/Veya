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
