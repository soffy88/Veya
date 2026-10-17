"""Bot lifecycle and execution API routes.

Independent Bot entry boundary — does NOT route through MasterAgent.

    POST /api/v1/bot-runtime              — create bot
    GET  /api/v1/bot-runtime              — list bots
    GET  /api/v1/bot-runtime/{bot_id}     — get bot
    POST /api/v1/bot-runtime/{bot_id}/deploy   — deploy new revision
    POST /api/v1/bot-runtime/{bot_id}/start    — start runtime
    POST /api/v1/bot-runtime/{bot_id}/pause    — pause runtime
    POST /api/v1/bot-runtime/{bot_id}/resume   — resume runtime
    POST /api/v1/bot-runtime/{bot_id}/stop     — stop runtime
    POST /api/v1/bot-runtime/{bot_id}/messages — independent entry (submit goal)
    POST /api/v1/bot-runtime/{bot_id}/goals    — submit goal
    GET  /api/v1/bot-runtime/{bot_id}/sessions — list sessions
    GET  /api/v1/bot-runtime/{bot_id}/goals/{goal_id} — get goal status
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from server import auth as auth_mod

router = APIRouter(
    prefix="/api/v1/bot-runtime",
    tags=["bot-runtime"],
    dependencies=[Depends(auth_mod.require_user)],
)


# ── Request / Response models ──────────────────────────────────────


class CreateBotRequest(BaseModel):
    bot_id: str
    #: Deprecated: the owner is always the signed-in user. Kept optional so
    #: existing clients keep working; a different value is rejected.
    owner_id: str = ""
    runtime_id: str
    name: str = ""
    description: str = ""
    instruction_refs: list[str] = []
    capability_refs: list[str] = []
    skill_refs: list[str] = []
    tool_refs: list[str] = []
    policy_refs: list[str] = []
    available_skill_ids: list[str] | None = None
    available_playbook_ids: list[str] | None = None


class DeployBotRequest(BaseModel):
    runtime_id: str | None = None
    revision: int = 2


class UpdateBotRequest(BaseModel):
    available_skill_ids: list[str] | None = None
    available_playbook_ids: list[str] | None = None
    config_version: int | None = None


class SubmitGoalRequest(BaseModel):
    objective: str
    session_id: str | None = None
    context: dict[str, Any] | None = None
    source: str = "external"


class MessageRequest(BaseModel):
    message: str
    session_id: str | None = None
    context: dict[str, Any] | None = None


class BotResponse(BaseModel):
    bot_id: str
    definition_id: str = ""
    definition_version: int = 0
    deployment_id: str = ""
    deployment_revision: int = 0
    runtime_id: str = ""
    instance_id: str = ""
    status: str = "ready"


class GoalResponse(BaseModel):
    bot_id: str
    goal_id: str = ""
    session_id: str = ""
    instance_id: str = ""
    status: str = "unknown"
    summary: str = ""
    artifacts: list[str] = []
    rounds: int = 0
    cost_usd: float = 0.0
    error: str = ""


# ── Ownership (W11 D2 pattern) ─────────────────────────────────────
# Every bot belongs to the user who created it. The owner always comes from
# the authenticated session, never from the request; anyone else gets 404 so
# bot ids are not enumerable.


_MANAGER: tuple[Any, Any] | None = None


def _definition_store_root() -> Path:
    """Where bot definitions/deployments live (next to the bot registry)."""
    raw = os.environ.get("VEYA_AGENT_DEFINITION_STORE_PATH")
    if raw:
        return Path(raw).expanduser()
    return Path.home() / ".veya"


def _manager() -> tuple[Any, Any]:
    """Process-wide BotManager.

    One manager per process so a runtime started by one request is the same
    runtime a later pause/resume/stop/message request finds. The registry is
    the shared ``BotRegistry`` default (``VEYA_BOT_REGISTRY_PATH`` or
    ``~/.veya/bot_registry.json``), the same one the bot team routes use.
    """
    global _MANAGER
    if _MANAGER is None:
        from server.agent_definition import AgentDefinitionStore
        from server.bot_runtime.manager import BotManager
        from server.goal_run.bot_identity import BotRegistry

        bot_registry = BotRegistry()
        definition_store = AgentDefinitionStore(_definition_store_root())
        _MANAGER = (BotManager(bot_registry, definition_store), bot_registry)
    return _MANAGER


def _user_id(user: dict[str, Any]) -> str:
    return str(user.get("user_id") or "").strip()


def _require_owned_bot(bot_registry: Any, bot_id: str, user: dict[str, Any]) -> None:
    spec = bot_registry.get(bot_id)
    if spec is None or str(spec.owner_id or "").strip() != _user_id(user):
        raise HTTPException(status_code=404, detail=f"Bot not found: {bot_id}")


# ── Bot management routes ──────────────────────────────────────────


@router.post("", response_model=BotResponse)
async def create_bot(
    req: CreateBotRequest, user: dict[str, Any] = Depends(auth_mod.require_user)
) -> BotResponse:
    """Create a new Bot with full identity chain."""
    if req.owner_id and req.owner_id != _user_id(user):
        raise HTTPException(status_code=403, detail="owner_id must be the signed-in user")
    manager, bot_registry = _manager()
    existing = bot_registry.get(req.bot_id)
    if existing is not None and str(existing.owner_id or "").strip() != _user_id(user):
        raise HTTPException(status_code=409, detail=f"bot_id already taken: {req.bot_id}")

    try:
        handle = manager.create_bot(
            bot_id=req.bot_id,
            owner_id=_user_id(user),
            runtime_id=req.runtime_id,
            name=req.name,
            description=req.description,
            instruction_refs=tuple(req.instruction_refs),
            capability_refs=tuple(req.capability_refs),
            skill_refs=tuple(req.skill_refs),
            tool_refs=tuple(req.tool_refs),
            policy_refs=tuple(req.policy_refs),
            available_skill_ids=req.available_skill_ids,
            available_playbook_ids=req.available_playbook_ids,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    return BotResponse(**handle.to_dict())


@router.get("", response_model=list[BotResponse])
async def list_bots(user: dict[str, Any] = Depends(auth_mod.require_user)) -> list[BotResponse]:
    """List the signed-in user's Bots."""
    manager, _ = _manager()
    handles = manager.list_bots(owner_id=_user_id(user))
    return [BotResponse(**h.to_dict()) for h in handles]


@router.get("/{bot_id}", response_model=BotResponse)
async def get_bot(
    bot_id: str, user: dict[str, Any] = Depends(auth_mod.require_user)
) -> BotResponse:
    """Get a Bot by ID."""
    manager, bot_registry = _manager()
    _require_owned_bot(bot_registry, bot_id, user)
    handle = manager.get_bot(bot_id)
    if handle is None:
        raise HTTPException(status_code=404, detail=f"Bot not found: {bot_id}")
    return BotResponse(**handle.to_dict())


@router.post("/{bot_id}/deploy", response_model=BotResponse)
async def deploy_bot(
    bot_id: str, req: DeployBotRequest, user: dict[str, Any] = Depends(auth_mod.require_user)
) -> BotResponse:
    """Deploy a new revision for an existing Bot."""
    manager, bot_registry = _manager()
    _require_owned_bot(bot_registry, bot_id, user)

    try:
        handle = manager.deploy_bot(bot_id, runtime_id=req.runtime_id, revision=req.revision)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    return BotResponse(**handle.to_dict())


@router.post("/{bot_id}/start")
async def start_bot(
    bot_id: str, user: dict[str, Any] = Depends(auth_mod.require_user)
) -> dict[str, Any]:
    """Start a Bot runtime instance."""
    manager, bot_registry = _manager()
    _require_owned_bot(bot_registry, bot_id, user)

    try:
        instance = await manager.start_bot(bot_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    return {"bot_id": bot_id, "instance_id": instance.instance_id, "status": instance.status.value}


@router.post("/{bot_id}/pause")
async def pause_bot(
    bot_id: str, user: dict[str, Any] = Depends(auth_mod.require_user)
) -> dict[str, Any]:
    """Pause a running Bot."""
    manager, bot_registry = _manager()
    _require_owned_bot(bot_registry, bot_id, user)

    try:
        await manager.pause_bot(bot_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    return {"bot_id": bot_id, "status": "paused"}


@router.post("/{bot_id}/resume")
async def resume_bot(
    bot_id: str, user: dict[str, Any] = Depends(auth_mod.require_user)
) -> dict[str, Any]:
    """Resume a paused Bot."""
    manager, bot_registry = _manager()
    _require_owned_bot(bot_registry, bot_id, user)

    try:
        await manager.resume_bot(bot_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    return {"bot_id": bot_id, "status": "resumed"}


@router.post("/{bot_id}/stop")
async def stop_bot(
    bot_id: str, user: dict[str, Any] = Depends(auth_mod.require_user)
) -> dict[str, Any]:
    """Stop a Bot runtime."""
    manager, bot_registry = _manager()
    _require_owned_bot(bot_registry, bot_id, user)

    try:
        await manager.stop_bot(bot_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    return {"bot_id": bot_id, "status": "stopped"}


# ── Goal submission routes ─────────────────────────────────────────


@router.post("/{bot_id}/messages", response_model=GoalResponse)
async def submit_message(
    bot_id: str, req: MessageRequest, user: dict[str, Any] = Depends(auth_mod.require_user)
) -> GoalResponse:
    """Independent Bot entry: submit a message/goal directly to the Bot.

    This route does NOT go through MasterAgent. It enters:
        BotManager → BotRuntime → AgentEngine → AgentLoop
    """
    manager, bot_registry = _manager()
    _require_owned_bot(bot_registry, bot_id, user)

    try:
        result = await manager.submit_goal(
            bot_id,
            req.message,
            session_id=req.session_id,
            context=req.context,
            source="external",
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    return GoalResponse(**result.to_dict())


@router.post("/{bot_id}/goals", response_model=GoalResponse)
async def submit_goal(
    bot_id: str, req: SubmitGoalRequest, user: dict[str, Any] = Depends(auth_mod.require_user)
) -> GoalResponse:
    """Submit a goal to a Bot for execution."""
    manager, bot_registry = _manager()
    _require_owned_bot(bot_registry, bot_id, user)

    try:
        result = await manager.submit_goal(
            bot_id,
            req.objective,
            session_id=req.session_id,
            context=req.context,
            source=req.source,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    return GoalResponse(**result.to_dict())


@router.get("/{bot_id}/sessions")
async def list_sessions(
    bot_id: str, user: dict[str, Any] = Depends(auth_mod.require_user)
) -> dict[str, Any]:
    """List sessions for a Bot."""
    manager, bot_registry = _manager()
    _require_owned_bot(bot_registry, bot_id, user)
    session_id = manager.get_session(bot_id)
    return {"bot_id": bot_id, "sessions": [session_id] if session_id else []}


@router.get("/{bot_id}/goals/{goal_id}")
async def get_goal(
    bot_id: str, goal_id: str, user: dict[str, Any] = Depends(auth_mod.require_user)
) -> dict[str, Any]:
    """Get a goal's status from the durable GoalRun store.

    GoalRun is the durable execution authority: every run writes
    ``<root>/.veya-project/goal-runs/<goal_id>/taskgraph.json``.  This route used
    to answer a hard-coded ``"status": "unknown"`` stub, so callers (and the
    frontend) could never observe whether a triggered routine actually ran.
    """
    _, bot_registry = _manager()
    _require_owned_bot(bot_registry, bot_id, user)

    from server.goal_run.paths import resolve_goal_run_root
    from server.goal_run.store import load_goal_run

    root = resolve_goal_run_root(None, bot_id=bot_id)
    state = load_goal_run(root, goal_id, bot_id=bot_id)
    if state is None:
        raise HTTPException(status_code=404, detail=f"goal {goal_id!r} not found")

    status = getattr(state, "status", None)
    status = getattr(status, "value", status) or "unknown"
    return {
        "bot_id": bot_id,
        "goal_id": goal_id,
        "status": str(status),
        "goal_text": str(getattr(state, "goal_text", "") or ""),
        "created_at": getattr(state, "created_at", None),
        "started_at": getattr(state, "started_at", None),
        "finished_at": getattr(state, "finished_at", None),
    }


__all__ = ["router"]
