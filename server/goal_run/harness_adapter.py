"""Backward-compatible bridge from canonical GoalRun to LongRunningHarness.

GoalRun remains the authority for task scheduling and terminal state.  This
adapter only projects the harness control state into GoalRun's existing
``runtime_checkpoint`` field and never creates a GoalRun or a computer.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from runtime.execution.long_running import (
    LongRunBudget,
    LongRunCheckpointStore,
    LongRunningHarness,
    LongRunState,
    ProgressObservation,
)


class GoalRunHarnessAdapter:
    def __init__(self, state: Any, project_root: str, harness: LongRunningHarness):
        self.goal_state = state
        self.project_root = project_root
        self.harness = harness

    @classmethod
    def attach(cls, state: Any, project_root: str) -> GoalRunHarnessAdapter:
        existing = state.runtime_checkpoint or {}
        projected = existing.get("long_running") if isinstance(existing, dict) else None
        run_dir = Path(project_root) / ".veya-project" / "goal-runs" / state.goal_id
        checkpoint_store = LongRunCheckpointStore(run_dir)
        if projected:
            harness_state = LongRunState(**projected)
        else:
            computer_id = str(
                (state.budget or {}).get("computer_id") or f"goalrun-computer:{state.goal_id}"
            )
            budget = LongRunBudget(
                max_wall_s=float((state.budget or {}).get("max_wall_s", 7200)),
                max_tool_calls=int((state.budget or {}).get("max_tool_calls", 1000)),
                max_replans=int((state.budget or {}).get("max_replans", 20)),
                max_retrievals=int((state.budget or {}).get("max_retrievals", 200)),
                max_context_tokens=int((state.budget or {}).get("max_context_tokens", 1_000_000)),
            )
            harness_state = LongRunState(
                goal_run_id=state.goal_id,
                computer_id=computer_id,
                plan=[task.instruction for task in state.tasks.values()],
            )
            harness = LongRunningHarness(harness_state, budget, checkpoint_store=checkpoint_store)
            return cls(state, project_root, harness)
        harness = LongRunningHarness(
            harness_state,
            checkpoint_store=checkpoint_store,
        )
        return cls(state, project_root, harness)

    @property
    def goal_run_id(self) -> str:
        return self.harness.state.goal_run_id

    @property
    def computer_id(self) -> str:
        return self.harness.state.computer_id

    def observe(self, *, task_id: str | None = None) -> bool:
        task_signature = [
            (task.id, task.status.value, tuple(task.artifacts), task.retries)
            for task in self.goal_state.tasks.values()
        ]
        artifacts = sorted(
            {artifact for task in self.goal_state.tasks.values() for artifact in task.artifacts}
        )
        state_hash = hashlib.sha256(
            json.dumps(task_signature, sort_keys=True, default=list).encode()
        ).hexdigest()
        return self.harness.record_progress(
            ProgressObservation(
                artifacts=artifacts,
                state_hash=state_hash,
                plan_step=task_id,
            )
        )

    def persist(self, *, reason: str = "goal_run") -> None:
        self.harness.checkpoint(reason=reason)
        checkpoint = dict(self.goal_state.runtime_checkpoint or {})
        checkpoint["long_running"] = asdict(self.harness.state)
        self.goal_state.runtime_checkpoint = checkpoint

    async def provider_call(self, request: Any, providers: list[str], **kwargs: Any) -> Any:
        result = await self.harness.provider_call(request, providers, **kwargs)
        self.persist(reason="provider_recovered")
        return result

    def apply_verification(self, outcome: str, **kwargs: Any) -> str:
        status = self.harness.apply_verification(outcome, **kwargs)
        self.persist(reason=f"verification_{str(outcome).lower()}")
        return status
