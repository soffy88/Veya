"""Trigger dispatch: event/schedule → Routine lookup → handoff record (P2-C).

The dispatcher is a pure routing step. It resolves enabled routines for a
trigger topic, consumes the trigger identity exactly once per routine, and
returns handoff records for the Persistent Veya Bot → MasterAgent → existing
canonical GoalRun path. It never executes, never finalizes, never creates a
GoalRun, and never starts a parallel run: all trigger state lands in the
passed-in GoalRunState.
"""

from __future__ import annotations

import time
from typing import Any

from runtime.execution.models import (
    ROUTINE_EXECUTION_AUTHORITY,
    assert_routine_execution_authority_zero,
)
from server.goal_run.models import (
    ROUTINE_MAX_STARTED,
    ROUTINE_TRIGGER_TOPICS,
    GoalRunState,
    RoutineSpec,
    RoutineState,
)

HANDOFF_VIA = ("veya-bot", "master-agent", "goal-run")


def trigger_identity(trigger_topic: str, trigger_id: str) -> str:
    """Stable identity for one trigger occurrence (idempotency key)."""
    return f"{trigger_topic}:{trigger_id}"


def dispatch_trigger(
    state: GoalRunState,
    specs: list[RoutineSpec],
    *,
    topic: str,
    trigger_id: str,
    payload: dict[str, Any] | None = None,
    now: float | None = None,
) -> list[dict[str, Any]]:
    """Consume one trigger and return handoff records for MasterAgent.

    Idempotent: a trigger identity already consumed by a routine is never
    started again, including after restart (state survives in taskgraph.json).
    Capped: at most ROUTINE_MAX_STARTED routines start per dispatch call.
    Same-GoalRun: a routine already bound to another GoalRun is refused.
    """
    assert_routine_execution_authority_zero()
    if ROUTINE_EXECUTION_AUTHORITY != 0:
        raise AssertionError("routine execution authority must stay 0")
    if topic not in ROUTINE_TRIGGER_TOPICS:
        raise ValueError(f"unknown trigger topic {topic!r}")
    if not trigger_id:
        raise ValueError("trigger_id is required")

    identity = trigger_identity(topic, trigger_id)
    moment = time.time() if now is None else now
    handoffs: list[dict[str, Any]] = []
    started = 0
    for spec in specs:
        if not spec.enabled or spec.trigger_topic != topic:
            continue
        entry = state.routine_states.get(spec.routine_id)
        if entry is not None and entry.canonical_goal_run_id not in (None, state.goal_id):
            raise ValueError(
                f"routine {spec.routine_id!r} belongs to GoalRun "
                f"{entry.canonical_goal_run_id!r}, not {state.goal_id!r}"
            )
        if entry is not None and identity in entry.consumed_trigger_ids:
            continue  # already consumed — never start twice.
        if started >= ROUTINE_MAX_STARTED:
            break
        if entry is None:
            entry = RoutineState(
                routine_id=spec.routine_id,
                canonical_goal_run_id=state.goal_id,
                version=spec.version,
            )
            state.routine_states[spec.routine_id] = entry
        entry.version = spec.version
        entry.consumed_trigger_ids.append(identity)
        entry.started_count += 1
        entry.last_trigger_ref = trigger_id
        entry.canonical_goal_run_id = state.goal_id
        entry.trigger_metadata = {"trigger_id": trigger_id, **(payload or {})}
        target: dict[str, Any] = {}
        if spec.target_skill_id:
            target = {"skill_id": spec.target_skill_id}
        elif spec.target_playbook_id:
            target = {"playbook_id": spec.target_playbook_id}
        else:
            target = {"goal_template": spec.goal_template}
        handoffs.append(
            {
                "routine_id": spec.routine_id,
                "version": spec.version,
                "trigger_topic": topic,
                "trigger_id": trigger_id,
                "trigger_identity": identity,
                "target": target,
                "goal_run_id": state.goal_id,
                "evidence_requirements": list(spec.evidence_requirements),
                "dispatched_at": moment,
                "via": list(HANDOFF_VIA),
            }
        )
        started += 1
    return handoffs


__all__ = ["HANDOFF_VIA", "dispatch_trigger", "trigger_identity"]
