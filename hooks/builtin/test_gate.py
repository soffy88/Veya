from __future__ import annotations

import sys

from oprim import bash_exec
from hooks.types import HookInput, HookOutput

# Use the current interpreter so pytest is always found (venv-safe)
_PYTHON = sys.executable


async def test_gate(inp: HookInput) -> HookOutput:
    """H4: run pytest after execute/build turn; block on failure."""
    res = bash_exec(
        f"{_PYTHON} -m pytest -x -q --tb=short",
        cwd=inp.cwd,
        timeout=300,
    )
    if hasattr(res, "code"):
        code = res.code
        stdout = (res.stdout or "") + (res.stderr or "")
    elif isinstance(res, dict):
        code = res.get("code", res.get("exit_code", 0))
        stdout = res.get("stdout", "")
    else:
        code = 0
        stdout = str(res)
    if code != 0:
        return HookOutput(
            decision="block",
            reason=f"Tests failed:\n{stdout[-2000:]}",
        )
    return HookOutput(decision="pass")
