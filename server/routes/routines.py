import uuid
import time

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from server import auth as auth_mod
from server.bot_team.routine_clock import _default_handoff_consumer
from server.goal_run.bot_identity import require_same_bot
from server.goal_run.models import RoutineSpec
from server.goal_run.routine_registry import RoutineRegistry
from server.goal_run.routine_history import RoutineHistoryStore

def _require_owned_bot(
    bot_id: str,
    user: dict = Depends(auth_mod.require_user),
) -> None:
    """W11 D2: routines belong to the bot's owner; anyone else gets 404."""

    from server.goal_run.bot_identity import BotRegistry
    from server.routes.bot_team import _require_bot

    _require_bot(BotRegistry(), bot_id, owner_id=user["user_id"])


router = APIRouter(
    prefix="/api/v1/bots/{bot_id}/routines",
    tags=["routines"],
    dependencies=[Depends(_require_owned_bot)],
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
    probe_result = run_probe(spec.probe, spec.workspace_path or None)
    
    # Record run history
    trigger_id = f"run_now:{uuid.uuid4().hex}"
    run_entry = {
        "trigger_id": trigger_id,
        "status": probe_result.get("status", "dispatched"),
        "probe": probe_result,
        "dispatched_at": time.time(),
    }
    if not probe_result["success"]:
        run_entry["status"] = "probe_error"
    elif probe_result["kind"] == "git_changes" and not probe_result["data"].get("has_changes", False):
        run_entry["status"] = "skipped"
    else:
        run_entry["status"] = "dispatched"

    try:
        RoutineHistoryStore(bot_id=bot_id).add_record(routine_id, run_entry)
    except Exception:
        pass
    
    # Skip dispatch if probe skipped or errored
    if not probe_result["success"] or (probe_result["kind"] == "git_changes" and not probe_result["data"].get("has_changes", False)):
        return {"triggered": False, "reason": run_entry["status"], "probe": probe_result}

    handoff = {
        "routine_id": routine_id,
        "trigger_topic": "schedule.trigger",
        "trigger_id": run_entry["trigger_id"],
        "target": {"goal_template": spec.objective},
        "bot_id": bot_id,
        "model": spec.model,
        "workspace_path": spec.workspace_path,
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
        history = RoutineHistoryStore(bot_id=bot_id).get_history(routine_id)
        if len(history) > limit:
            history = history[-limit:]
        return {"history": history}
    except Exception:
        return {"history": []}
