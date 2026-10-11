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
        # W11 D2: a named bot is visible only to its owner. Shared system bots
        # (Chef, the default bot, or bots registered without an owner) stay
        # reachable by every signed-in user. A foreign bot answers 404, never 403,
        # so its existence does not leak.
        shared = spec.bot_id in (DEFAULT_BOT_ID, CHEF_BOT_ID) or spec.owner_id in ("", "system")
        if owner_id and not shared and spec.owner_id != owner_id:
            return None
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
        "model_source": spec.model_source,
        "model_source_config": spec.model_source_config,
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
    model_source: str | None = Field(default=None, max_length=32)
    model_source_config: dict | None = None


class BotPatchRequest(BaseModel):
    name: str | None = Field(default=None, max_length=120)
    description: str | None = Field(default=None, max_length=400)
    instructions: str | None = Field(default=None, max_length=4000)
    provider: str | None = Field(default=None, max_length=128)
    model: str | None = Field(default=None, max_length=256)
    runner_id: str | None = Field(default=None, max_length=128)
    available_skill_ids: list[str] | None = None
    available_playbook_ids: list[str] | None = None
    model_source: str | None = Field(default=None, max_length=32)
    model_source_config: dict | None = None


class BotMessageRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=20_000)
    workspace_id: str | None = Field(default=None, max_length=512)
    #: Optional client-supplied message id (NANO-W10 item 1). When present it is
    #: the idempotency key, so a client retry never opens a second turn.
    message_id: str | None = Field(default=None, max_length=200)


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
    user: dict[str, Any] = Depends(auth_mod.require_user),
) -> dict[str, Any]:
    """Create a group chat of 1..6 member bots (all of them the caller's)."""

    registry = bot_registry()
    if len(req.bot_ids) > MAX_GROUP_MEMBERS:
        raise HTTPException(
            status_code=422,
            detail=f"a group may have at most {MAX_GROUP_MEMBERS} members",
        )
    unknown = [
        bot_id
        for bot_id in req.bot_ids
        if _resolve_bot(registry, bot_id, owner_id=user["user_id"]) is None
    ]
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


def _group_visible(group: Any, user_id: str) -> bool:
    """W11 D2: a group belongs to whoever owns every member bot.

    Groups carry no owner of their own; deriving it from the members keeps one
    source of truth (the bot registry) and needs no schema change.
    """

    registry = bot_registry()
    members = list(getattr(group, "bot_ids", None) or [])
    return bool(members) and all(
        _resolve_bot(registry, bot_id, owner_id=user_id) is not None for bot_id in members
    )


def _require_group(group_id: str, user_id: str) -> Any:
    group = groups().get(group_id)
    if group is None or not _group_visible(group, user_id):
        raise HTTPException(status_code=404, detail=f"group {group_id!r} not found")
    return group


@router.get("/groups")
async def list_groups(
    user: dict[str, Any] = Depends(auth_mod.require_user),
) -> dict[str, Any]:
    return {
        "groups": [
            group.to_dict() for group in groups().list() if _group_visible(group, user["user_id"])
        ]
    }


@router.get("/groups/{group_id}/messages")
async def get_group_messages(
    group_id: str,
    include_hidden: bool = False,
    user: dict[str, Any] = Depends(auth_mod.require_user),
) -> dict[str, Any]:
    """The group transcript. Hidden PASS replies are excluded by default."""

    _require_group(group_id, user["user_id"])
    return {
        "group_id": group_id,
        "messages": groups().messages(group_id, include_hidden=include_hidden),
    }


@router.post("/groups/{group_id}/messages")
async def post_group_message(
    group_id: str,
    req: GroupMessageRequest,
    async_mode: bool = Query(False, alias="async"),
    user: dict[str, Any] = Depends(auth_mod.require_user),
) -> Any:
    """Offer every member one turn per round, in order, then stop.

    ``?async=1`` answers ``202`` with the round handle instead of holding the
    request open: the round still runs through the same product path, and the
    transcript endpoint below is where the replies appear.
    """

    group = _require_group(group_id, user["user_id"])
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
    user: dict[str, Any] = Depends(auth_mod.require_user),
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
    user: dict[str, Any] = Depends(auth_mod.require_user),
) -> dict[str, Any]:
    """List the bots. Chef is seeded here on the first call."""

    registry = bot_registry()
    ensure_chef_seeded(registry, owner_id=user["user_id"])
    return {
        "bots": [_bot_view(spec) for spec in registry.list() if spec.owner_id in (user["user_id"], "", "system", "default") or spec.bot_id in (DEFAULT_BOT_ID, CHEF_BOT_ID)],
        "default_bot_id": DEFAULT_BOT_ID,
        "chef_bot_id": CHEF_BOT_ID,
    }


@router.post("")
async def create_bot(
    req: BotCreateRequest,
    user: dict[str, Any] = Depends(auth_mod.require_user),
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

    model_source = (req.model_source or "server").strip()
    if model_source not in ("server", "user_key", "cli"):
        raise HTTPException(
            status_code=422, detail="model_source must be 'server', 'user_key', or 'cli'"
        )

    spec = BotSpec(
        bot_id=bot_id,
        owner_id=user["user_id"],
        name=(req.name or bot_id).strip(),
        description=(req.description or "").strip(),
        instructions=(req.instructions or "").strip(),
        provider=(req.provider or "").strip(),
        model=(req.model or "").strip(),
        runner_id="local",
        model_source=model_source,
        model_source_config=dict(req.model_source_config or {}),
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
    user: dict[str, Any] = Depends(auth_mod.require_user),
) -> dict[str, Any]:
    return {"bot": _bot_view(_require_bot(bot_registry(), bot_id, owner_id=user["user_id"]))}


@router.patch("/{bot_id}")
async def patch_bot(
    bot_id: str,
    req: BotPatchRequest,
    user: dict[str, Any] = Depends(auth_mod.require_user),
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

    # Handle model_source validation
    model_source = updates.get("model_source")
    if model_source is not None:
        model_source = str(model_source).strip()
        if model_source not in ("server", "user_key", "cli"):
            raise HTTPException(
                status_code=422, detail="model_source must be 'server', 'user_key', or 'cli'"
            )
    else:
        model_source = spec.model_source

    model_source_config = updates.get("model_source_config")
    if model_source_config is not None:
        model_source_config = dict(model_source_config)
    else:
        model_source_config = spec.model_source_config

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
        model_source=model_source,
        model_source_config=model_source_config,
    )
    try:
        registry.register(updated)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(status_code=503, detail="bot registry is not writable") from exc
    return {"status": "updated", "bot": _bot_view(updated)}


@router.delete("/{bot_id}")
async def delete_bot(
    bot_id: str,
    user: dict[str, Any] = Depends(auth_mod.require_user),
) -> dict[str, Any]:
    """Remove one bot and forget its conversation."""

    if bot_id == DEFAULT_BOT_ID:
        raise HTTPException(status_code=422, detail="the default bot cannot be deleted")
    if bot_id == CHEF_BOT_ID:
        # Chef is seeded on every list; deleting it would only bring it back.
        raise HTTPException(status_code=422, detail="Chef cannot be deleted")
    registry = bot_registry()
    _require_bot(registry, bot_id, owner_id=user["user_id"])
    if not registry.unregister(bot_id):
        raise HTTPException(status_code=404, detail=f"bot {bot_id!r} not found")
    conversations().clear(bot_id)
    return {"status": "deleted", "bot_id": bot_id}


@router.get("/{bot_id}/messages")
async def get_bot_messages(
    bot_id: str,
    include_hidden: bool = False,
    user: dict[str, Any] = Depends(auth_mod.require_user),
) -> dict[str, Any]:
    """One bot's conversation. Hidden PASS replies are excluded by default."""

    # Resolve through the registry so an unknown (or someone else's) bot is 404.
    _require_bot(bot_registry(), bot_id, owner_id=user["user_id"])
    return {
        "bot_id": bot_id,
        "messages": conversations().list(bot_id, include_hidden=include_hidden),
    }


def _intake_through_turn_queue(
    conversation_ref: str,
    req: BotMessageRequest,
    user: dict[str, Any],
) -> Any | None:
    """Run an inbound bot message through the item-1 turn queue (W10 item 1).

    With ``VEYA_TURN_QUEUE`` off this returns ``None`` immediately and the route
    behaves exactly as before — the queue is an in-front layer, not a new path.
    With the flag on it enforces the three rules the spec asks for at the one
    point where a turn opens:

    * an idempotent retry (``message_id`` already seen) is dropped;
    * a direct message arriving while a turn is **running** becomes a steer;
    * messages inside the coalescing window are merged into the one turn that
      the queue then hands to the caller.

    Returns a JSONResponse when the queue short-circuits the normal path, or
    ``None`` to let the request proceed to the ordinary turn.
    """

    from server.nano_w10.turn_queue import get_turn_queue, turn_queue_enabled

    if not turn_queue_enabled():
        return None
    queue = get_turn_queue()
    decision = queue.intake(
        conversation_ref=conversation_ref,
        text=req.text,
        kind="direct",
        external_id=str(req.message_id or "").strip(),
        bot_ref=conversation_ref,
        user_directed=True,
    )
    action = str(decision.get("action") or "")
    if action == "drop":
        # Idempotent retry: answer with the turn we already opened.
        return JSONResponse(
            status_code=200,
            content={
                "status": "duplicate",
                "bot_id": conversation_ref,
                "action": "drop",
                "reason": decision.get("reason", "already received"),
            },
        )
    if action == "steer":
        # A direct message mid-turn steers it; it does not wait for it.
        return JSONResponse(
            status_code=202,
            content={
                "status": "steered",
                "bot_id": conversation_ref,
                "action": "steer",
                "state": decision.get("state"),
                "reason": decision.get("reason"),
            },
        )
    if action == "queue":
        pending = queue.next_turn(conversation_ref)
        if pending and int(pending.get("count", 0)) > 1:
            # Coalesced burst: run ONE turn over the merged members. This mutates
            # ``req`` in place so the caller below uses the merged text; it is a
            # local request object and is not shared.
            req.text = str(pending.get("text") or req.text)
    return None


@router.post("/{bot_id}/messages")
async def post_bot_message(
    bot_id: str,
    req: BotMessageRequest,
    async_mode: bool = Query(False, alias="async"),
    user: dict[str, Any] = Depends(auth_mod.require_user),
) -> Any:
    """Talk to one bot: record the message, run its turn, record the reply.

    ``?async=1`` opens the turn, answers ``202`` with its ``task_id`` and the
    endpoints that carry the result, then finishes the turn in the background.
    The synchronous behaviour (the default) is unchanged.
    """

    spec = _require_bot(bot_registry(), bot_id, owner_id=user["user_id"])
    queued = _intake_through_turn_queue(spec.bot_id, req, user)
    if queued is not None:
        return queued
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


class BotCorrectionRequest(BaseModel):
    """A correction typed while the bot's turn is still running (W4).

    This is not a new message channel: it lands through the item-1 turn queue's
    steer, so a correction and an inbound message follow the same path.
    """

    text: str = Field(..., min_length=1, max_length=4_000)


@router.post("/{bot_id}/corrections")
async def post_bot_correction(
    bot_id: str,
    req: BotCorrectionRequest,
    user: dict[str, Any] = Depends(auth_mod.require_user),
) -> Any:
    """Steer a running bot turn with a correction (W4).

    Auth is **required** (``require_user`` → 401 when anonymous) and the bot
    must belong to the caller (the same ``_require_bot(owner_id=...)`` guard
    every other bot endpoint uses), so one account cannot steer another
    account's bot. The correction lands through the item-1 turn queue's
    ``steer``; with no running turn it is refused rather than silently opening
    a new one.
    """

    from server.nano_w10.live_stream import get_live_stream
    from server.nano_w10.turn_queue import get_turn_queue, turn_queue_enabled

    spec = _require_bot(bot_registry(), bot_id, owner_id=user["user_id"])
    if not turn_queue_enabled():
        raise HTTPException(
            status_code=409,
            detail={"code": "FEATURE_DISABLED", "flag": "VEYA_TURN_QUEUE"},
        )
    queue = get_turn_queue()
    # A correction is only meaningful while a turn is live. If none is running
    # it is refused (the user can send a normal message instead) rather than
    # silently becoming a queued turn.
    if queue.turn_state(spec.bot_id) != "running":
        return JSONResponse(
            status_code=409,
            content={
                "status": "rejected",
                "bot_id": spec.bot_id,
                "action": "reject",
                "reason": "no running turn to correct",
                "hint": "该 bot 当前没有正在运行的一轮对话，请直接发消息。",
            },
        )
    result = queue.steer(spec.bot_id, req.text.strip())
    if result.get("status") == "failed":
        return JSONResponse(status_code=409, content=result)
    get_live_stream().correction(spec.bot_id, req.text.strip())
    return {
        "status": "steered",
        "bot_id": spec.bot_id,
        "action": result.get("action"),
        "state": result.get("state"),
        "reason": result.get("reason"),
    }


class CheckpointVerdictRequest(BaseModel):
    """The user's answer to a W5 finish checkpoint card."""

    verdict: str = Field(..., description="accept | send_back")
    note: str = Field("", max_length=4_000, description="required when sending the card back")


@router.post("/{bot_id}/checkpoints/{card_id}")
async def post_checkpoint_verdict(
    bot_id: str,
    card_id: str,
    req: CheckpointVerdictRequest,
    user: dict[str, Any] = Depends(auth_mod.require_user),
) -> Any:
    """Accept or send back a finish checkpoint card (W5).

    Auth is **required** (``require_user``) and the bot must belong to the
    caller, so one account can never answer another account's card. The card
    must also exist **in that bot's own conversation** — the existing message
    log is the record of what was posted, so a guessed card id is a 404 rather
    than an accepted verdict. ``send_back`` lands the note through the item-1
    turn queue: a card sent back is a correction, not a second channel.
    """

    from server.nano_w10.loop_mode import CHECKPOINT_VERDICTS, get_finish_checkpoint_gate

    spec = _require_bot(bot_registry(), bot_id, owner_id=user["user_id"])
    verdict = str(req.verdict or "").strip().lower()
    if verdict not in CHECKPOINT_VERDICTS:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "INVALID_INPUT",
                "reason": f"verdict must be one of {list(CHECKPOINT_VERDICTS)}",
            },
        )
    store = conversations()
    owned = any(
        str((message.get("meta") or {}).get("card_id") or "") == card_id
        for message in store.list(spec.bot_id, include_hidden=True)
    )
    if not owned:
        raise HTTPException(status_code=404, detail="该检查卡不在这个 bot 的对话里")

    result = get_finish_checkpoint_gate().respond(
        spec.bot_id,
        card_id,
        verdict,
        note=req.note,
    )
    if result.get("status") == "failed":
        code = str((result.get("error") or {}).get("code") or "")
        status = 409 if code == "FEATURE_DISABLED" else 422
        return JSONResponse(status_code=status, content=result)
    return {
        "status": "recorded",
        "bot_id": spec.bot_id,
        "card_id": card_id,
        "verdict": verdict,
        "action": result.get("landed") or verdict,
    }


@router.post("/{bot_id}/message_bot")
async def post_bot_to_bot_message(
    bot_id: str,
    req: BotToBotMessageRequest,
    user: dict[str, Any] = Depends(auth_mod.require_user),
) -> dict[str, Any]:
    """Let one bot message another through the existing delegation runtime."""

    # Both ends must be the caller's bots (W11 D2).
    _require_bot(bot_registry(), bot_id, owner_id=user["user_id"])
    if req.target_bot_id != bot_id and _resolve_bot(
        bot_registry(), req.target_bot_id, owner_id=user["user_id"]
    ) is None:
        raise HTTPException(status_code=422, detail=f"unknown target bot {req.target_bot_id!r}")
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
