"""H5 PostResult — audit/filter before aggregated result leaves coordinator."""

from __future__ import annotations

from hooks.types import HookInput, HookOutput


async def post_result_hook(inp: HookInput) -> HookOutput:
    """Strip internal fields and emit audit log entry."""
    result = inp.result
    if not isinstance(result, dict):
        return HookOutput(decision="pass")

    # Strip internal _upstream context from output before returning to caller
    cleaned = {k: v for k, v in result.items() if not k.startswith("_")}
    return HookOutput(decision="pass", modified_result=cleaned)
