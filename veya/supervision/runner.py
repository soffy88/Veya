"""Canonical mission runner — the single execution leg for every supervision mode.

It delegates to the project's ONE dispatch entry (``server.project_ask`` →
GoalRun → hicode/dsh/builtin); it never executes work itself and never invents a
verdict. The returned object is a neutral projection of what actually ran.
"""

from __future__ import annotations

import types
from collections.abc import Awaitable, Callable
from typing import Any

Dispatch = Callable[[str, str], Awaitable[str]]

# Executors the canonical project dispatch understands (spec §17/§18).
_KNOWN_EXECUTORS = {"hicode", "dsh", "worker", "builtin", "native_tool"}


def executor_hint(mission: Any) -> str | None:
    """Executor pinned on the mission (``policies.execution_policy.assignee_hint``).

    The mission owns *what* to run and on which executor; ``project_ask`` still owns
    *how* to run it. No routing decision is invented here — an unknown/absent value
    simply means "let the canonical entry decide".
    """
    policies = getattr(mission, "policies", None)
    execution = getattr(policies, "execution_policy", None) or {}
    hint = str(execution.get("assignee_hint") or "").strip().lower()
    return hint if hint in _KNOWN_EXECUTORS else None


# internal alias kept for the runner's own call sites / tests
_assignee_hint = executor_hint


def _default_dispatch(project_root: str, request: str) -> Awaitable[str]:
    from server.project_ask import project_ask

    return project_ask(project_root=project_root, request=request)


def _dispatch_for(assignee_hint: str | None) -> Dispatch:
    """Canonical dispatch, optionally pinned to one executor."""
    if not assignee_hint:
        return _default_dispatch

    def _pinned(project_root: str, request: str) -> Awaitable[str]:
        from server.project_ask import project_ask

        return project_ask(project_root=project_root, request=request, assignee_hint=assignee_hint)

    return _pinned


async def canonical_runner(mission: Any, *, dispatch: Dispatch | None = None) -> Any:
    """Run one mission iteration through the canonical project dispatch."""

    run = dispatch or _dispatch_for(_assignee_hint(mission))
    text = await run(str(mission.workspace or ""), str(mission.goal))
    return types.SimpleNamespace(
        goal_id=None,
        status="executed",
        tasks={},
        final_summary=str(text),
        unfinished_work=[],
    )


def runner_with(dispatch: Dispatch) -> Callable[[Any], Awaitable[Any]]:
    async def _run(mission: Any) -> Any:
        return await canonical_runner(mission, dispatch=dispatch)

    return _run


__all__ = ["canonical_runner", "runner_with"]
