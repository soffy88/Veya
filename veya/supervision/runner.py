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

# Executors that have a real canonical project execution path.  ``builtin`` is
# retained as a truthful semantic-only capability; it must never be selected
# for a mutation task.  ``worker``/``native_tool`` are not aliases for this
# substrate and therefore cannot be admitted here.
_KNOWN_EXECUTORS = {"hicode", "dsh", "builtin"}


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
    """Run one mission iteration through the canonical GoalRun substrate.

    A mission result is projected from the durable GoalRun state, never from
    executor prose.  The semantic-only builtin capability is explicitly
    fail-closed for this project-mutation path.
    """

    hint = _assignee_hint(mission)
    if dispatch is not None:
        text = await dispatch(str(mission.workspace or ""), str(mission.goal))
        return types.SimpleNamespace(
            goal_id=None,
            status="executed",
            tasks={},
            final_summary=str(text),
            unfinished_work=[],
        )

    if hint == "builtin":
        return types.SimpleNamespace(
            goal_id=None,
            status="blocked",
            tasks={},
            final_summary=(
                "builtin is semantic-only and cannot execute project mutation, shell, or git "
                "effects"
            ),
            unfinished_work=["no executable mutation capability assigned"],
        )

    if hint not in {"hicode", "dsh"}:
        return types.SimpleNamespace(
            goal_id=None,
            status="blocked",
            tasks={},
            final_summary=f"no executable canonical L1 executor assigned: {hint!r}",
            unfinished_work=["canonical executor capability is unresolved"],
        )

    from server.goal_run.canonical_worker import CanonicalWorkerAdapter
    from server.goal_run.runner import project_run_goal
    from server.goal_run.store import load_goal_run

    task = {
        "id": f"{mission.mission_id}-q4",
        "title": str(mission.goal)[:120],
        "instruction": str(mission.goal),
        "acceptance": list(getattr(mission, "acceptance_criteria", []) or [])
        or ["execution produced verifiable evidence"],
        "assignee": hint,
        "depends_on": [],
    }
    # Existing typed adapter: skip only the advisory plan gate for an already
    # admitted supervision mission; execution remains the canonical leaf path.
    integration_adapter = CanonicalWorkerAdapter.for_capability(
        task_id=str(task["id"]),
        objective=str(mission.goal),
        capability=None,
        verification_required=True,
    )
    integration_adapter.skip_plan_review = True
    response = await project_run_goal(
        project_root=str(mission.workspace or ""),
        goal=str(mission.goal),
        tasks=[task],
        mode="act_eager",
        wait=True,
        verification_required=True,
        integration_adapter=integration_adapter,
    )
    state = load_goal_run(str(mission.workspace or ""), response.goal_id)
    if state is not None:
        return state
    return types.SimpleNamespace(
        goal_id=response.goal_id,
        status=str(response.status),
        tasks={},
        final_summary=response.summary
        or response.block_reason
        or "GoalRun produced no durable state",
        unfinished_work=[response.block_reason] if response.block_reason else [],
    )


def _execution_mode(mission: Any) -> str:
    """Explicit L2 execution mode (``execution_policy.mode``). No auto-guessing."""

    policies = getattr(mission, "policies", None)
    execution = getattr(policies, "execution_policy", None) or {}
    return str(execution.get("mode") or "").strip().lower()


def runner_with(dispatch: Dispatch) -> Callable[[Any], Awaitable[Any]]:
    async def _run(mission: Any) -> Any:
        return await canonical_runner(mission, dispatch=dispatch)

    return _run


def select_runner(
    *,
    orchestrated_dispatch: Any = None,
    orchestrated_decompose: Any = None,
) -> Callable[[Any], Awaitable[Any]]:
    """One runner selection for the ONE MissionLoop.

    ``execution_policy.mode=veya_orchestrated`` routes to the L2 orchestration
    scheduler over the L1 substrate; every other mode keeps the existing
    canonical project dispatch. No second loop, no shadow state.
    """

    async def _run(mission: Any) -> Any:
        if _execution_mode(mission) == "veya_orchestrated" and orchestrated_dispatch is not None:
            from .orchestrated import orchestrated_runner

            return await orchestrated_runner(
                mission,
                dispatch=orchestrated_dispatch,
                decompose=orchestrated_decompose,
            )
        return await canonical_runner(mission)

    return _run


__all__ = ["canonical_runner", "executor_hint", "runner_with", "select_runner"]
