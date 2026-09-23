"""GoalRun adapter for the legacy Board card execution surface.

BoardStore/Card remain the Kanban projection and worktree metadata store.  The
GoalRun created here owns the durable task lifecycle; this adapter only binds
the existing engine provider to one opaque GoalRun leaf.
"""

from __future__ import annotations

from typing import Any

from server.goal_run.leaf import LeafResult
from server.goal_run.store import save_goal_run


class BoardGoalRunAdapter:
    verification_required = True
    skip_plan_review = True
    # Code review is advisory evidence only.  It must not make a typed Board
    # GoalRun depend on the ambient LLM before it can reach a terminal state.
    skip_advisory_code_review = True

    def __init__(self, *, card: Any, project_root: str):
        self.card = card
        self.project_root = project_root

    async def before_execution(self, state: Any, project_root: str) -> None:
        return None

    async def before_iteration(self, state: Any, project_root: str, task: Any) -> None:
        return None

    def checkpoint(self, state: Any, project_root: str, *, reason: str) -> None:
        save_goal_run(state, project_root)

    async def execute_semantic_task(self, state: Any, task: Any) -> LeafResult:
        from server.engine_runner import run_engine

        result = await run_engine(
            self.card.engine,
            self.card.prompt,
            model=self.card.model or None,
            cwd=self.card.worktree or None,
            timeout_s=900.0,
        )
        output = str(result.get("output", ""))[:4000]
        error = str(result.get("error", ""))[:2000]
        if result.get("ok"):
            return LeafResult(
                status="completed",
                summary=output,
                stop_reason="completed",
            )
        return LeafResult(
            status="blocked",
            summary=output,
            block_reason=error or "board engine failed",
            stop_reason="exception",
        )


__all__ = ["BoardGoalRunAdapter"]
