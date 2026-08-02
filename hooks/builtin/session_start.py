"""H7 SessionStart — initialise per-session context (cwd, cost baseline)."""
from __future__ import annotations

import os

from hooks.types import HookInput, HookOutput


async def session_start_hook(inp: HookInput) -> HookOutput:
    """Inject working directory and reset cost baseline into session context."""
    ctx = dict(inp.context)
    if "cwd" not in ctx:
        ctx["cwd"] = os.getcwd()
    if "cost_baseline" not in ctx:
        ctx["cost_baseline"] = 0.0
    return HookOutput(decision="pass", modified_result=ctx)
