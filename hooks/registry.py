from __future__ import annotations

from collections.abc import Callable

from hooks.builtin.permission import permission_hook
from hooks.builtin.post_result import post_result_hook
from hooks.builtin.pre_dispatch import pre_dispatch_hook
from hooks.builtin.redact import redact_hook
from hooks.builtin.session_start import session_start_hook
from hooks.builtin.squad_stop import squad_stop_hook
from hooks.builtin.test_gate import test_gate

_EXECUTE_PERSONAS = {"build", "execute"}


def build_hook_chain(point: str, persona: str) -> list[Callable]:
    """Return hook callables for a given cut-point and persona."""
    chains: dict[str, list[Callable]] = {
        "pre_dispatch": [pre_dispatch_hook],
        "pre_tool": [permission_hook],
        "post_tool": [redact_hook],
        "pre_result": [test_gate] if persona in _EXECUTE_PERSONAS else [],
        "post_result": [post_result_hook],
        "squad_stop": [squad_stop_hook],
        "session_start": [session_start_hook],
    }
    return chains.get(point, [])


async def run_hook_chain(
    point: str,
    persona: str,
    inp,
) -> tuple[str, str, object]:
    """Run all hooks for a point; return (decision, reason, modified_result).

    Decision precedence: halt > block > modify > pass.
    """
    from hooks.types import HookOutput

    hooks = build_hook_chain(point, persona)
    decision = "pass"
    reason = ""
    modified = inp.result

    for hook in hooks:
        out: HookOutput = await hook(inp)
        if out.modified_result is not None:
            modified = out.modified_result
        if out.decision in ("halt", "block") and decision not in ("halt",):
            decision = out.decision
            reason = out.reason
        if out.decision == "halt":
            break

    return decision, reason, modified


def build_coordinator_hooks() -> dict[str, list[Callable]]:
    """Coordinator-level hooks: H1 PreDispatch / H5 PostResult."""
    return {
        "pre_dispatch": [pre_dispatch_hook],
        "post_result": [post_result_hook],
    }
