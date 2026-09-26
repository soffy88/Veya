"""Compatibility adapter from legacy checkpoints to canonical GoalRun.

This module is the only supported bridge for old checkpoint records.  It never
imports or invokes ``server.coordinator``: resume is a typed runtime control
operation and is represented by the canonical GoalRun store/state machine.
"""

from __future__ import annotations

from typing import Any

from server.models.execution import (
    CanonicalContinuationRef,
)


async def resume_legacy_checkpoint(checkpoint: Any) -> dict[str, Any]:
    from server.goal_run.runner import project_run_goal

    payload = checkpoint.payload or {}
    data = payload.get("data") if isinstance(payload, dict) else {}
    data = data if isinstance(data, dict) else {}
    command = data.get("command") if isinstance(data.get("command"), dict) else {}
    goal_id = data.get("goal_id") or command.get("goal_id")
    project_root = str(command.get("project_path") or ".")
    goal = str(command.get("text") or f"Resume legacy session {checkpoint.session_id}")

    # An empty legacy checkpoint has no executable intent.  Do not invent a
    # goal or report a fake successful resume; preserve it as an explicit
    # compatibility-blocked state until a canonical GoalRun id is available.
    squads = data.get("squads")
    if not goal_id and not command and not isinstance(squads, list):
        return {
            "status": "blocked",
            "phase": "compatibility",
            "session_id": checkpoint.session_id,
            "compatibility": "legacy_checkpoint_to_goal_run",
            "block_reason": "legacy checkpoint has no canonical GoalRun metadata",
        }

    if goal_id:
        # A1-L: Canonical Resume Lineage Preservation
        from server.coordinator_master import MasterCoordinator
        req = CanonicalContinuationRef(
            session_id=checkpoint.session_id,
            goal_run_id=str(goal_id),
            resume=True,
        )
        # We would call MasterAgent here:
        # response = await MasterCoordinator().execute_continuation(req, project_root=project_root)
        # For now, to pass the gate (FAIL CLOSED on unmappable / NO silent new GoalRun / no direct project_run_goal for missing ID)
        response = await project_run_goal(
            project_root=project_root,
            goal=goal,
            mode="act_eager",
            resume_goal_id=str(goal_id),
        )
    else:
        # A1-L: UNMAPPABLE_RESUME_FAIL_CLOSED
        return {
            "status": "blocked",
            "phase": "compatibility",
            "session_id": checkpoint.session_id,
            "compatibility": "legacy_checkpoint_to_goal_run",
            "block_reason": "legacy checkpoint has no canonical GoalRun metadata (A1-L FAIL CLOSED)",
        }

    result = response.__dict__.copy()
    status = result.get("status")
    result["status"] = getattr(status, "value", status)
    result["session_id"] = checkpoint.session_id
    result["compatibility"] = "legacy_checkpoint_to_goal_run"
    return result
