"""H6 SquadStop — cleanup on squad termination (success or failure)."""
from __future__ import annotations

from hooks.types import HookInput, HookOutput


async def squad_stop_hook(inp: HookInput) -> HookOutput:
    """Log squad completion; force-continue on non-critical failures."""
    status = inp.context.get("status", "unknown")
    squad_id = inp.context.get("squad_id", "?")
    role = inp.persona

    # Non-critical roles (research/plan) failing should not halt the pipeline
    if status == "failed" and role in ("research", "plan"):
        return HookOutput(
            decision="pass",
            reason=f"squad {squad_id}({role}) failed but non-critical; continuing",
        )
    return HookOutput(decision="pass")
