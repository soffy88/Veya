import uuid

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from server.bot_team.routine_clock import _default_handoff_consumer
from server.goal_run.bot_identity import require_same_bot
from server.goal_run.models import RoutineSpec
from server.goal_run.routine_registry import RoutineRegistry

router = APIRouter(prefix="/api/v1/bots/{bot_id}/routines", tags=["routines"])

def _get_registry(bot_id: str) -> RoutineRegistry:
    return RoutineRegistry(bot_id=bot_id)

class RoutineCreateRequest(BaseModel):
    objective: str
    schedule: str
    timezone: str = "Asia/Shanghai"
    enabled: bool = True
    routine_id: str | None = None

class RoutinePatchRequest(BaseModel):
    objective: str | None = None
    schedule: str | None = None
    timezone: str | None = None
    enabled: bool | None = None

@router.get("/")
def list_routines(bot_id: str):
    registry = _get_registry(bot_id)
    return registry.list(include_disabled=True)

@router.post("/")
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
        bot_id=bot_id
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

    handoff = {
        "routine_id": routine_id,
        "trigger_topic": "schedule.trigger",
        "trigger_id": f"run_now:{uuid.uuid4().hex}",
        "target": {"goal_template": spec.objective},
        "bot_id": bot_id,
    }
    await _default_handoff_consumer([handoff])
    return [handoff]
