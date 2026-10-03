"""§26 Autonomous Continuation — combines existing primitives.

Autonomous execution = GoalRun + ContinuationTrigger + Budget + Verification + NoProgressProtection.
There is no separate AutonomousAgent engine.

Limits constrain execution but do not define completion.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any

from server.goal_run.continuation import ContinuationTriggerManager
from server.no_progress import NoProgressVerdict, ProgressTracker


@dataclass
class AutonomyPolicy:
    """Policy for autonomous execution.

    Limits constrain execution but do not define completion.
    """

    wall_clock_budget: float | None = None
    token_budget: int | None = None
    cost_budget: float | None = None
    continuation_policy: str = "COALESCE"
    verification_policy: str = "required"
    intervention_policy: str = "on_no_progress"
    max_continuations: int = 10

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class AutonomousState:
    """State of an autonomous execution."""

    goal_run_id: str
    policy: AutonomyPolicy
    trigger_manager: ContinuationTriggerManager | None = None
    progress_tracker: ProgressTracker | None = None
    continuations_used: int = 0
    started_at: float = field(default_factory=time.time)
    tokens_used: int = 0
    cost_incurred: float = 0.0

    @property
    def wall_clock_exhausted(self) -> bool:
        if self.policy.wall_clock_budget is None:
            return False
        return (time.time() - self.started_at) >= self.policy.wall_clock_budget

    @property
    def token_budget_exhausted(self) -> bool:
        if self.policy.token_budget is None:
            return False
        return self.tokens_used >= self.policy.token_budget

    @property
    def cost_budget_exhausted(self) -> bool:
        if self.policy.cost_budget is None:
            return False
        return self.cost_incurred >= self.policy.cost_budget

    @property
    def budget_exhausted(self) -> bool:
        return (
            self.wall_clock_exhausted
            or self.token_budget_exhausted
            or self.cost_budget_exhausted
        )

    @property
    def continuations_exhausted(self) -> bool:
        return self.continuations_used >= self.policy.max_continuations

    def to_dict(self) -> dict[str, Any]:
        return {
            "goal_run_id": self.goal_run_id,
            "policy": self.policy.to_dict(),
            "continuations_used": self.continuations_used,
            "started_at": self.started_at,
            "tokens_used": self.tokens_used,
            "cost_incurred": self.cost_incurred,
            "budget_exhausted": self.budget_exhausted,
            "continuations_exhausted": self.continuations_exhausted,
        }


def new_autonomous_state(
    goal_run_id: str,
    policy: AutonomyPolicy | None = None,
) -> AutonomousState:
    """Create a new autonomous execution state."""
    return AutonomousState(
        goal_run_id=goal_run_id,
        policy=policy or AutonomyPolicy(),
        progress_tracker=ProgressTracker(goal_run_id=goal_run_id),
    )


def should_continue(state: AutonomousState) -> tuple[bool, str]:
    """Determine if autonomous execution should continue.

    Budget exhaustion is NOT completion — it is interruption.
    """
    if state.budget_exhausted:
        return False, "budget_exhausted"
    if state.continuations_exhausted:
        return False, "continuations_exhausted"
    if state.progress_tracker:
        verdict = state.progress_tracker.tick()
        if verdict is NoProgressVerdict.NO_PROGRESS_DETECTED:
            return False, "no_progress_detected"
    return True, "ok"
