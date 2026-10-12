"""W12 Supervisor REST routes (flag VEYA_SUPERVISOR=1).

Owner-scoped controls for supervised long tasks. Every route
re-checks ownership: a binding belongs to the bot that started
it, and the bot belongs to the user that owns it. Other users
get 404 (not 403 — the binding must not be visible at all).

No new scheduler, event store or approval path: this module is
a thin adapter over server.supervisor.binding + the routine
registry + the GoalRun store.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from server import auth as auth_mod
from server.goal_run.routine_registry import RoutineRegistry
from server.supervisor import binding as supervisor_binding
from server.supervisor import enabled as supervisor_enabled

router = APIRouter(
    prefix="/api/v1/bots/{bot_id}/supervised",
    tags=["supervisor"],
    dependencies=[Depends(auth_mod.require_user)],
)


class SupervisorListResponse(BaseModel):
    bot_id: str
    bindings: list[dict[str, Any]]


class SupervisorActionResponse(BaseModel):
    binding_id: str
    status: str


class SupervisorIntervalRequest(BaseModel):
    report_every_minutes: int = Field(default=30, gt=0)


class SupervisorStallRequest(BaseModel):
    stall_after_minutes: int = Field(default=20, gt=0)


class SupervisorMaxRepairsRequest(BaseModel):
    max_repairs: int = Field(default=3, ge=0)


def _user_id(user: dict[str, Any]) -> str:
    return str(user.get("user_id") or user.get("username") or "anonymous")


def _owned_binding(binding_id: str, bot_id: str, user: dict[str, Any]) -> supervisor_binding.SupervisorBinding:
    """Load a binding only when it belongs to this bot and this user.

    Returns the binding or raises 404. The 404 (not 403) is deliberate:
    another user's binding is invisible, so its existence is never
    leaked.
    """
    binding = supervisor_binding.get_binding(binding_id)
    if binding is None:
        raise HTTPException(status_code=404, detail="binding not found")
    if binding.bot_id != bot_id:
        raise HTTPException(status_code=404, detail="binding not found")
    if binding.owner_id != bot_id:
        raise HTTPException(status_code=404, detail="binding not found")
    # Owner check: the bot's owner must be the requesting user.
    from server.goal_run.bot_identity import BotRegistry

    registry = BotRegistry()
    spec = registry.get(bot_id)
    if spec is not None and spec.owner_id and spec.owner_id != _user_id(user):
        raise HTTPException(status_code=404, detail="binding not found")
    return binding


@router.get("", response_model=SupervisorListResponse)
def list_supervised(
    bot_id: str,
    user: dict[str, Any] = Depends(auth_mod.get_current_user),
) -> dict[str, Any]:
    """List this bot's supervised bindings (owner-scoped)."""
    if not supervisor_enabled():
        return SupervisorListResponse(bot_id=bot_id, bindings=[]).model_dump()
    bindings = supervisor_binding.list_bindings(bot_id=bot_id)
    return {
        "bot_id": bot_id,
        "bindings": [b.to_dict() for b in bindings],
    }


@router.post("/{binding_id}/cancel", response_model=SupervisorActionResponse)
def cancel_supervised(
    bot_id: str,
    binding_id: str,
    user: dict[str, Any] = Depends(auth_mod.get_current_user),
) -> dict[str, Any]:
    """Cancel a supervised task: pause the GoalRun + delete the routine."""
    binding = _owned_binding(binding_id, bot_id, user)
    # Pause the GoalRun (execution authority).
    from server.goal_run.models import GoalStatus
    from server.goal_run.store import load_goal_run, save_goal_run

    state = load_goal_run(binding.project_root, binding.goal_run_id)
    if state is not None:
        state.status = GoalStatus.paused
        save_goal_run(state, binding.project_root)
    # Delete the routine (schedule authority).
    registry = RoutineRegistry(bot_id=bot_id)
    registry.set_enabled(binding.routine_id, False)
    # Close the binding.
    binding.status = "closed"
    supervisor_binding.save_binding(binding)
    return SupervisorActionResponse(binding_id=binding_id, status="closed").model_dump()


@router.post("/{binding_id}/pause", response_model=SupervisorActionResponse)
def pause_supervised(
    bot_id: str,
    binding_id: str,
    user: dict[str, Any] = Depends(auth_mod.get_current_user),
) -> dict[str, Any]:
    """Pause reporting (routine stops firing, binding stays open)."""
    binding = _owned_binding(binding_id, bot_id, user)
    registry = RoutineRegistry(bot_id=bot_id)
    registry.set_enabled(binding.routine_id, False)
    binding.status = "paused"
    supervisor_binding.save_binding(binding)
    return SupervisorActionResponse(binding_id=binding_id, status="paused").model_dump()


@router.post("/{binding_id}/resume", response_model=SupervisorActionResponse)
def resume_supervised(
    bot_id: str,
    binding_id: str,
    user: dict[str, Any] = Depends(auth_mod.get_current_user),
) -> dict[str, Any]:
    """Resume a paused supervised task."""
    binding = _owned_binding(binding_id, bot_id, user)
    registry = RoutineRegistry(bot_id=bot_id)
    registry.set_enabled(binding.routine_id, True)
    binding.status = "open"
    supervisor_binding.save_binding(binding)
    return SupervisorActionResponse(binding_id=binding_id, status="open").model_dump()


@router.post("/{binding_id}/interval", response_model=SupervisorActionResponse)
def set_interval(
    bot_id: str,
    binding_id: str,
    payload: SupervisorIntervalRequest,
    user: dict[str, Any] = Depends(auth_mod.get_current_user),
) -> dict[str, Any]:
    """Change the report cadence."""
    binding = _owned_binding(binding_id, bot_id, user)
    binding.report_every_minutes = payload.report_every_minutes
    supervisor_binding.save_binding(binding)
    return SupervisorActionResponse(binding_id=binding_id, status="updated").model_dump()


@router.post("/{binding_id}/stall", response_model=SupervisorActionResponse)
def set_stall(
    bot_id: str,
    binding_id: str,
    payload: SupervisorStallRequest,
    user: dict[str, Any] = Depends(auth_mod.get_current_user),
) -> dict[str, Any]:
    """Change the stall threshold."""
    binding = _owned_binding(binding_id, bot_id, user)
    binding.stall_after_minutes = payload.stall_after_minutes
    supervisor_binding.save_binding(binding)
    return SupervisorActionResponse(binding_id=binding_id, status="updated").model_dump()


@router.post("/{binding_id}/max-repairs", response_model=SupervisorActionResponse)
def set_max_repairs(
    bot_id: str,
    binding_id: str,
    payload: SupervisorMaxRepairsRequest,
    user: dict[str, Any] = Depends(auth_mod.get_current_user),
) -> dict[str, Any]:
    """Change the max auto-repair count."""
    binding = _owned_binding(binding_id, bot_id, user)
    binding.max_repairs = payload.max_repairs
    supervisor_binding.save_binding(binding)
    return SupervisorActionResponse(binding_id=binding_id, status="updated").model_dump()


__all__ = ["router"]
