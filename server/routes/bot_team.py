"""Veya bot team endpoints (Lorca P1/P2).

``/api/v1/bots`` is the multi-bot shell. It exposes the same product path as
``/api/v1/bot`` (the default bot): every turn it starts is a product task for
that bot, executed by the existing GoalRun semantic loop. These endpoints add
no scheduler, no task store and no executor — they only bind identity,
conversations and routing.
"""

from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from server import auth as auth_mod
from server.bot_team.async_turns import start_bot_turn_async, start_group_turn_async
from server.bot_team.chef import (
    CHEF_BOT_ID,
    ProposalStore,
    ensure_chef_seeded,
    proposal_store,
    record_team_proposal,
)
from server.bot_team.conversations import ConversationStore
from server.bot_team.groups import (
    DEFAULT_ROUNDS_PER_MESSAGE,
    MAX_GROUP_MEMBERS,
    MAX_ROUNDS_PER_MESSAGE,
    GroupStore,
    run_group_turn,
)
from server.bot_team.messaging import MAX_HOP_DEPTH, BotMessageRefused, BotMessenger
from server.bot_team.push import announce_bot_reply, announce_group_round
from server.bot_team.runners import RunnerRegistry
from server.bot_team.turns import run_bot_turn
from server.goal_run.bot_identity import DEFAULT_BOT_ID, BotRegistry, BotSpec

router = APIRouter(prefix="/api/v1/bots", tags=["bots"])

_BOT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


# ── shared helpers ───────────────────────────────────────────────────────


def bot_registry() -> BotRegistry:
    """The one bot catalog (``VEYA_BOT_REGISTRY_PATH`` overrides its file)."""

    return BotRegistry()


def conversations() -> ConversationStore:
    return ConversationStore()


def groups() -> GroupStore:
    return GroupStore()


def proposals() -> ProposalStore:
    return proposal_store()


def _default_bot_spec(owner_id: str) -> BotSpec:
    """The default bot, synthesised when the catalog does not list it.

    The default bot keeps working exactly as before; this only lets the new
    surface drive it without writing a record nobody asked for.
    """

    return BotSpec(
        bot_id=DEFAULT_BOT_ID,
        owner_id=owner_id,
        name="Veya Bot",
        description="The default Veya bot.",
        runner_id="local",
    )


def _resolve_bot(registry: BotRegistry, bot_id: str, *, owner_id: str = "") -> BotSpec | None:
    spec = registry.get(bot_id)
    if spec is not None:
        return spec
    if bot_id == DEFAULT_BOT_ID:
        return _default_bot_spec(owner_id)
    return None


def _require_bot(registry: BotRegistry, bot_id: str, *, owner_id: str = "") -> BotSpec:
    spec = _resolve_bot(registry, bot_id, owner_id=owner_id)
    if spec is None:
        raise HTTPException(status_code=404, detail=f"bot {bot_id!r} not found")
    return spec


def _bot_view(spec: BotSpec) -> dict[str, Any]:
    return {
        "bot_id": spec.bot_id,
        "name": spec.name or spec.bot_id,
        "description": spec.description,
        "instructions": spec.instructions,
        "provider": spec.provider or None,
        "model": spec.model or None,
        "runner_id": spec.runner_id,
        "owner_id": spec.owner_id,
        "config_version": spec.config_version,
        "knowledge_namespace": spec.knowledge_namespace,
        "context_namespace": spec.context_namespace,
        "routine_registry_ref": spec.routine_registry_ref,
        "available_skill_ids": list(spec.available_skill_ids),
        "available_playbook_ids": list(spec.available_playbook_ids),
    }


# ── request bodies ───────────────────────────────────────────────────────


class BotCreateRequest(BaseModel):
    bot_id: str = Field(..., min_length=1, max_length=64)
    name: str | None = Field(default=None, max_length=120)
    description: str | None = Field(default=None, max_length=400)
    instructions: str | None = Field(default=None, max_length=4000)
    provider: str | None = Field(default=None, max_length=128)
    model: str | None = Field(default=None, max_length=256)
    owner_id: str | None = Field(default=None, max_length=256)


class BotPatchRequest(BaseModel):
    name: str | None = Field(default=None, max_length=120)
    description: str | None = Field(default=None, max_length=400)
    instructions: str | None = Field(default=None, max_length=4000)
    provider: str | None = Field(default=None, max_length=128)
    model: str | None = Field(default=None, max_length=256)
    runner_id: str | None = Field(default=None, max_length=128)
    available_skill_ids: list[str] | None = None
    available_playbook_ids: list[str] | None = None


class BotMessageRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=20_000)
    workspace_id: str | None = Field(default=None, max_length=512)


class BotToBotMessageRequest(BaseModel):
    target_bot_id: str = Field(..., min_length=1, max_length=64)
    text: str = Field(..., min_length=1, max_length=20_000)
    depth: int = Field(default=0, ge=0, le=MAX_HOP_DEPTH)


class GroupCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    bot_ids: list[str] = Field(..., min_length=1)
    rounds_per_message: int = Field(
        default=DEFAULT_ROUNDS_PER_MESSAGE, ge=1, le=MAX_ROUNDS_PER_MESSAGE
    )


class GroupMessageRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=20_000)
    workspace_id: str | None = Field(default=None, max_length=512)


# ── groups and proposals are declared before ``/{bot_id}`` so a static path
# ── segment (``/groups``) wins over the bot-id path parameter.


@router.post("/groups")
async def create_group(
    req: GroupCreateRequest,
    user: dict[str, Any] = Depends(auth_mod.get_current_user),
) -> dict[str, Any]:
    """Create a group chat of 1..6 member bots."""

    del user
    registry = bot_registry()
    if len(req.bot_ids) > MAX_GROUP_MEMBERS:
        raise HTTPException(
            status_code=422,
            detail=f"a group may have at most {MAX_GROUP_MEMBERS} members",
        )
    unknown = [bot_id for bot_id in req.bot_ids if registry.get(bot_id) is None]
    if unknown:
        raise HTTPException(status_code=422, detail=f"unknown member bots: {unknown}")
    try:
        group = groups().create(
            name=req.name,
            bot_ids=list(req.bot_ids),
            rounds_per_message=req.rounds_per_message,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"status": "created", "group": group.to_dict()}


@router.get("/groups")
async def list_groups() -> dict[str, Any]:
    return {"groups": [group.to_dict() for group in groups().list()]}


@router.get("/groups/{group_id}/messages")
async def get_group_messages(group_id: str, include_hidden: bool = False) -> dict[str, Any]:
    """The group transcript. Hidden PASS replies are excluded by default."""

    if groups().get(group_id) is None:
        raise HTTPException(status_code=404, detail=f"group {group_id!r} not found")
    return {
        "group_id": group_id,
        "messages": groups().messages(group_id, include_hidden=include_hidden),
    }


@router.post("/groups/{group_id}/messages")
async def post_group_message(
    group_id: str,
    req: GroupMessageRequest,
    async_mode: bool = Query(False, alias="async"),
    user: dict[str, Any] = Depends(auth_mod.get_current_user),
) -> Any:
    """Offer every member one turn per round, in order, then stop.

    ``?async=1`` answers ``202`` with the round handle instead of holding the
    request open: the round still runs through the same product path, and the
    transcript endpoint below is where the replies appear.
    """

    group = groups().get(group_id)
    if group is None:
        raise HTTPException(status_code=404, detail=f"group {group_id!r} not found")
    if async_mode:
        ref = await start_group_turn_async(
            group=group,
            text=req.text,
            user=dict(user),
            registry=bot_registry(),
            conversations=conversations(),
            workspace_id=req.workspace_id,
        )
        return JSONResponse(status_code=202, content=ref.to_dict())
    try:
        result = await run_group_turn(
            group=group,
            text=req.text,
            user=dict(user),
            registry=bot_registry(),
            conversations=conversations(),
            workspace_id=req.workspace_id,
        )
    except OSError as exc:
        raise HTTPException(status_code=503, detail="bot conversation is not writable") from exc
    announce_group_round(
        group_name=group.name,
        group_id=group.id,
        replies=result.get("replies") or [],
        user_id=str(user.get("user_id") or ""),
    )
    return {"status": "completed", **result}


@router.post("/proposals/{proposal_id}/accept")
async def accept_proposal(
    proposal_id: str,
    user: dict[str, Any] = Depends(auth_mod.get_current_user),
) -> dict[str, Any]:
    """Create the bots Chef proposed. Nothing is created without this call."""

    store = proposals()
    if store.get(proposal_id) is None:
        raise HTTPException(status_code=404, detail=f"proposal {proposal_id!r} not found")
    try:
        accepted = store.accept(proposal_id, bot_registry(), owner_id=user["user_id"])
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"status": accepted["status"], **accepted}


# ── bot collection ───────────────────────────────────────────────────────


@router.get("")
async def list_bots(
    user: dict[str, Any] = Depends(auth_mod.get_current_user),
) -> dict[str, Any]:
    """List the bots. Chef is seeded here on the first call."""

    registry = bot_registry()
    ensure_chef_seeded(registry, owner_id=user["user_id"])
    return {
        "bots": [_bot_view(spec) for spec in registry.list()],
        "default_bot_id": DEFAULT_BOT_ID,
        "chef_bot_id": CHEF_BOT_ID,
    }


@router.post("")
async def create_bot(
    req: BotCreateRequest,
    user: dict[str, Any] = Depends(auth_mod.get_current_user),
) -> dict[str, Any]:
    """Register one named bot."""

    bot_id = req.bot_id.strip()
    if not _BOT_ID_RE.match(bot_id):
        raise HTTPException(
            status_code=422,
            detail="bot_id must be lowercase snake_case (letters, digits, '.', '_', '-')",
        )
    registry = bot_registry()
    if registry.get(bot_id) is not None:
        raise HTTPException(status_code=409, detail=f"bot {bot_id!r} already exists")
    spec = BotSpec(
        bot_id=bot_id,
        owner_id=(req.owner_id or user["user_id"]).strip(),
        name=(req.name or bot_id).strip(),
        description=(req.description or "").strip(),
        instructions=(req.instructions or "").strip(),
        provider=(req.provider or "").strip(),
        model=(req.model or "").strip(),
        runner_id="local",
    )
    try:
        registry.register(spec)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(status_code=503, detail="bot registry is not writable") from exc
    return {"status": "created", "bot": _bot_view(spec)}


# ── one bot ──────────────────────────────────────────────────────────────


@router.get("/{bot_id}")
async def get_bot(
    bot_id: str,
    user: dict[str, Any] = Depends(auth_mod.get_current_user),
) -> dict[str, Any]:
    return {"bot": _bot_view(_require_bot(bot_registry(), bot_id, owner_id=user["user_id"]))}


@router.patch("/{bot_id}")
async def patch_bot(
    bot_id: str,
    req: BotPatchRequest,
    user: dict[str, Any] = Depends(auth_mod.get_current_user),
) -> dict[str, Any]:
    """Update the configuration of one bot (name/description/instructions/LLM)."""

    registry = bot_registry()
    # ``owner_id`` matters here: patching the default bot (which is not in the
    # catalog yet) registers it, and a registry record needs an owner.
    spec = _require_bot(registry, bot_id, owner_id=user["user_id"])
    updates = req.model_dump(exclude_unset=True)
    if not updates:
        raise HTTPException(status_code=422, detail="no fields to update")
    # Lorca (P6): a bot may only be pointed at a runner that exists here, so a
    # typo cannot turn every future turn into an unreachable-runner error.
    requested_runner = str(updates.get("runner_id") or "").strip()
    if (
        requested_runner
        and requested_runner != "local"
        and RunnerRegistry().get(requested_runner) is None
    ):
        raise HTTPException(
            status_code=422,
            detail=f"runner {requested_runner!r} is not registered",
        )
    updated = BotSpec(
        bot_id=spec.bot_id,
        owner_id=spec.owner_id,
        config_version=spec.config_version,
        knowledge_namespace=spec.knowledge_namespace,
        context_namespace=spec.context_namespace,
        routine_registry_ref=spec.routine_registry_ref,
        available_skill_ids=[
            str(item) for item in updates.get("available_skill_ids", spec.available_skill_ids)
        ],
        available_playbook_ids=[
            str(item) for item in updates.get("available_playbook_ids", spec.available_playbook_ids)
        ],
        name=str(updates.get("name", spec.name)),
        description=str(updates.get("description", spec.description)),
        instructions=str(updates.get("instructions", spec.instructions)),
        provider=str(updates.get("provider", spec.provider)),
        model=str(updates.get("model", spec.model)),
        runner_id=str(updates.get("runner_id", spec.runner_id)),
    )
    try:
        registry.register(updated)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(status_code=503, detail="bot registry is not writable") from exc
    return {"status": "updated", "bot": _bot_view(updated)}


@router.delete("/{bot_id}")
async def delete_bot(bot_id: str) -> dict[str, Any]:
    """Remove one bot and forget its conversation."""

    if bot_id == DEFAULT_BOT_ID:
        raise HTTPException(status_code=422, detail="the default bot cannot be deleted")
    if bot_id == CHEF_BOT_ID:
        # Chef is seeded on every list; deleting it would only bring it back.
        raise HTTPException(status_code=422, detail="Chef cannot be deleted")
    registry = bot_registry()
    if not registry.unregister(bot_id):
        raise HTTPException(status_code=404, detail=f"bot {bot_id!r} not found")
    conversations().clear(bot_id)
    return {"status": "deleted", "bot_id": bot_id}


@router.get("/{bot_id}/messages")
async def get_bot_messages(bot_id: str, include_hidden: bool = False) -> dict[str, Any]:
    """One bot's conversation. Hidden PASS replies are excluded by default."""

    # Resolve through the registry so an unknown bot is 404, not an empty log.
    _require_bot(bot_registry(), bot_id)
    return {
        "bot_id": bot_id,
        "messages": conversations().list(bot_id, include_hidden=include_hidden),
    }


@router.post("/{bot_id}/messages")
async def post_bot_message(
    bot_id: str,
    req: BotMessageRequest,
    async_mode: bool = Query(False, alias="async"),
    user: dict[str, Any] = Depends(auth_mod.get_current_user),
) -> Any:
    """Talk to one bot: record the message, run its turn, record the reply.

    ``?async=1`` opens the turn, answers ``202`` with its ``task_id`` and the
    endpoints that carry the result, then finishes the turn in the background.
    The synchronous behaviour (the default) is unchanged.
    """

    spec = _require_bot(bot_registry(), bot_id, owner_id=user["user_id"])
    if async_mode:
        ref = await start_bot_turn_async(
            bot=spec,
            text=req.text,
            user=dict(user),
            workspace_id=req.workspace_id,
            conversations=conversations(),
        )
        return JSONResponse(status_code=202, content=ref.to_dict())
    try:
        turn = await run_bot_turn(
            bot=spec,
            text=req.text,
            user=dict(user),
            workspace_id=req.workspace_id,
            conversations=conversations(),
        )
    except OSError as exc:
        raise HTTPException(status_code=503, detail="bot conversation is not writable") from exc
    response: dict[str, Any] = {
        "status": turn.status,
        "bot_id": spec.bot_id,
        # Lorca (P6): where the turn actually ran. "local" is this server.
        "runner_id": spec.runner_id,
        "task_id": turn.task_id,
        "session_id": turn.session_id,
        "reply": turn.reply,
        "trace_id": turn.trace_id,
        # A remote turn's task lives on the runner, so its id means nothing to
        # this server's workbench.
        "workbench_url": f"/workbench/{turn.task_id}" if spec.runner_id == "local" else None,
    }
    announce_bot_reply(
        bot_name=spec.name or spec.bot_id,
        reply=turn.reply,
        task_id=turn.task_id,
        user_id=str(user.get("user_id") or ""),
        bot_id=spec.bot_id,
    )
    if spec.bot_id == CHEF_BOT_ID:
        # Only Chef proposes a team, and the proposal is only stored here:
        # creating the bots still needs an explicit accept.
        proposal = record_team_proposal(
            bot_id=spec.bot_id,
            text=turn.reply,
            task_id=turn.task_id,
            owner_id=user["user_id"],
        )
        if proposal is not None:
            response["proposal"] = {
                "id": proposal["id"],
                "bots": proposal["bots"],
                "accept_url": f"/api/v1/bots/proposals/{proposal['id']}/accept",
            }
    return response


@router.post("/{bot_id}/message_bot")
async def post_bot_to_bot_message(
    bot_id: str,
    req: BotToBotMessageRequest,
    user: dict[str, Any] = Depends(auth_mod.get_current_user),
) -> dict[str, Any]:
    """Let one bot message another through the existing delegation runtime."""

    messenger = BotMessenger(bot_registry(), conversations=conversations())
    try:
        outcome = await messenger.message_bot(
            bot_id,
            req.target_bot_id,
            req.text,
            user=dict(user),
            depth=req.depth,
        )
    except BotMessageRefused as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(status_code=503, detail="bot conversation is not writable") from exc
    return {"status": outcome.status, **outcome.to_dict()}


__all__ = ["router"]
