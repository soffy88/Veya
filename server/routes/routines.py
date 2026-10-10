import uuid
import time

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from server import auth as auth_mod
from server.bot_team.routine_clock import _default_handoff_consumer
from server.goal_run.bot_identity import require_same_bot
from server.goal_run.models import RoutineSpec
from server.goal_run.routine_registry import RoutineRegistry
from server.goal_run.project_store import ProjectStore

router = APIRouter(
    prefix="/api/v1/bots/{bot_id}/routines",
    tags=["routines"],
    dependencies=[Depends(auth_mod.get_current_user)],
)


def _get_registry(bot_id: str) -> RoutineRegistry:
    return RoutineRegistry(bot_id=bot_id)


class RoutineCreateRequest(BaseModel):
    objective: str
    schedule: str
    timezone: str = "Asia/Shanghai"
    enabled: bool = False  # ORCA-W4: default to disabled on create
    routine_id: str | None = None
    probe: dict | None = None  # ORCA-W4: probe spec
    model: str | None = None   # ORCA-W4: per-routine model
    workspace_path: str | None = None  # ORCA-W4: workspace binding


class RoutinePatchRequest(BaseModel):
    objective: str | None = None
    schedule: str | None = None
    timezone: str | None = None
    enabled: bool | None = None
    probe: dict | None = None  # ORCA-W4: probe spec
    model: str | None = None   # ORCA-W4: per-routine model
    workspace_path: str | None = None  # ORCA-W4: workspace binding


@router.get("")
@router.get("/", include_in_schema=False)
def list_routines(bot_id: str):
    registry = _get_registry(bot_id)
    return registry.list(include_disabled=True)


@router.post("")
@router.post("/", include_in_schema=False)
def create_routine(bot_id: str, req: RoutineCreateRequest):
    registry = _get_registry(bot_id)
    rid = req.routine_id or uuid.uuid4().hex

    spec = RoutineSpec(
        routine_id=rid,
        trigger_topic="schedule.trigger",
        objective=req.objective,
        goal_template=req.objective,
        schedule=req.schedule,
        timezone=req.timezone,
        enabled=req.enabled,
        bot_id=bot_id,
        probe=req.probe or {"kind": "none"},
        model=req.model,
        workspace_path=req.workspace_path,
    )
    registry.register(spec)
    return spec


@router.patch("/{routine_id}")
def patch_routine(bot_id: str, routine_id: str, req: RoutinePatchRequest):
    registry = _get_registry(bot_id)
    spec = registry.get(routine_id)
    if not spec:
        raise HTTPException(status_code=404, detail="Routine not found")

    require_same_bot(spec.bot_id, bot_id, f"routine:{routine_id}")

    if req.objective is not None:
        spec.objective = req.objective
        spec.goal_template = req.objective
    if req.schedule is not None:
        spec.schedule = req.schedule
    if req.timezone is not None:
        spec.timezone = req.timezone
    if req.enabled is not None:
        spec.enabled = req.enabled
    if req.probe is not None:
        spec.probe = req.probe
    if req.model is not None:
        spec.model = req.model
    if req.workspace_path is not None:
        spec.workspace_path = req.workspace_path

    registry.register(spec)  # this saves it
    return spec


@router.delete("/{routine_id}")
def delete_routine(bot_id: str, routine_id: str):
    registry = _get_registry(bot_id)
    spec = registry.get(routine_id)
    if not spec:
        raise HTTPException(status_code=404, detail="Routine not found")

    require_same_bot(spec.bot_id, bot_id, f"routine:{routine_id}")

    # RoutineRegistry doesn't have a delete method according to our grep, wait... let's check.
    # Let's remove from _items and save
    if routine_id in registry._items:
        del registry._items[routine_id]
        registry._save()
        return {"deleted": True}
    return {"deleted": False}


@router.post("/{routine_id}/run_now")
async def run_now(bot_id: str, routine_id: str):
    registry = _get_registry(bot_id)
    spec = registry.get(routine_id)
    if not spec:
        raise HTTPException(status_code=404, detail="Routine not found")

    require_same_bot(spec.bot_id, bot_id, f"routine:{routine_id}")

    # ORCA-W4: run_now works even on disabled routines
    # Run probe first
    from server.goal_run.routine_dispatch import run_probe
    probe_result = run_probe(spec)
    
    # Record run history
    try:
        goal_run_state = ProjectStore(bot_id=bot_id).load_goal_run_state()
        routine_state = goal_run_state.routine_states.get(routine_id)
        if routine_state is not None:
            trigger_id = f"run_now:{uuid.uuid4().hex}"
            run_entry = {
                "trigger_id": trigger_id,
                "status": probe_result.get("status", "dispatched"),
                "probe_result": probe_result,
                "at": time.time(),
            }
            routine_state.run_history.append(run_entry)
            if len(routine_state.run_history) > 50:
                routine_state.run_history = routine_state.run_history[-50:]
            ProjectStore(bot_id=bot_id).save_goal_run_state(goal_run_state)
    except Exception:
        pass
    
    # Skip dispatch if probe skipped or errored
    if probe_result.get("status") in ("skipped", "probe_error"):
        return {"triggered": False, "reason": probe_result.get("status"), "probe_result": probe_result}

    handoff = {
        "routine_id": routine_id,
        "trigger_topic": "schedule.trigger",
        "trigger_id": f"run_now:{uuid.uuid4().hex}",
        "target": {"goal_template": spec.objective},
        "bot_id": bot_id,
    }
    await _default_handoff_consumer([handoff])
    return {"triggered": True, "handoff": handoff}


@router.get("/{routine_id}/history")
def get_history(bot_id: str, routine_id: str, limit: int = 50):
    registry = _get_registry(bot_id)
    spec = registry.get(routine_id)
    if not spec:
        raise HTTPException(status_code=404, detail="Routine not found")

    require_same_bot(spec.bot_id, bot_id, f"routine:{routine_id}")

    try:
        goal_run_state = ProjectStore(bot_id=bot_id).load_goal_run_state()
        routine_state = goal_run_state.routine_states.get(routine_id)
        if routine_state is None:
            return {"history": []}
        history = routine_state.run_history[-limit:] if routine_state.run_history else []
        return {"history": history}
    except Exception:
        return {"history": []}
