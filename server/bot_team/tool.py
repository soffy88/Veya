"""``message_bot`` — the one model-visible bot-team tool (Lorca, approved).

Exactly one new tool surface: ``message_bot(target_bot_id, text)``. It sends a
message to another bot on the team and returns that bot's reply.

Two things come from the execution context and are never model arguments:

* **the source bot** — the bot whose GoalRun is running right now
  (:func:`server.goal_run.bot_identity.current_bot_id`). A model cannot name a
  source, so it cannot make one bot speak as another.
* **the hop depth of the chain that got here** — incremented for the target's
  turn, so the existing hop limit (:data:`~server.bot_team.messaging.MAX_HOP_DEPTH`)
  stays exact across nested calls instead of resetting on every hop.

Everything else is the P2 service: ``BotMessenger.message_bot`` owns the
self-target, unknown-bot and hop-limit refusals, the ``SpawnGuard`` budget, and
the delegation runtime that runs the target's turn. This module adds no second
messenger and no second entry into execution.
"""

from __future__ import annotations

import contextvars
import json
from typing import Any

from server.bot_team.messaging import MAX_HOP_DEPTH, BotMessageRefused, BotMessenger
from server.goal_run.bot_identity import BotRegistry, current_bot_id

#: Hop depth of the bot-message chain executing in this request context. Zero
#: means "this is the first bot message of the turn"; the tool increments it for
#: the target's turn so a nested call sees the real chain length.
_hop_depth_ctx: contextvars.ContextVar[int] = contextvars.ContextVar(
    "veya_bot_message_hop_depth", default=0
)

#: The tool's own name, re-exported so tests and the registry agree on it.
TOOL_NAME = "message_bot"

_TOOL_DESCRIPTION = (
    "Send a message to another bot on your team and get its reply back. The "
    "target bot runs one of its own turns to answer as its one job, then its "
    "reply is returned here. Use it when another bot on the team owns the work "
    "you are being asked about. target_bot_id must be a bot on the team: you "
    "cannot message yourself, a bot outside the team is refused, and a chain of "
    f"bot messages may not be deeper than {MAX_HOP_DEPTH} hops."
)

_TOOL_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "target_bot_id": {
            "type": "string",
            "description": (
                "The bot_id of the teammate you are messaging (the same ids the "
                "bots API lists, e.g. 'chef' or 'researcher')."
            ),
        },
        "text": {
            "type": "string",
            "description": ("The message in plain text: what you need that bot to do or answer."),
        },
    },
    "required": ["target_bot_id", "text"],
    "additionalProperties": False,
}


def current_hop_depth() -> int:
    """How many bot messages already led to the running turn."""

    return _hop_depth_ctx.get()


def bind_hop_depth(depth: int) -> contextvars.Token:
    """Bind the hop depth the target's turn inherits (the chain leading to it)."""

    return _hop_depth_ctx.set(int(depth))


def reset_hop_depth(token: contextvars.Token) -> None:
    _hop_depth_ctx.reset(token)


async def message_bot(target_bot_id: str, text: str) -> str:
    """Send a message to another bot on the team and return its reply.

    The source bot is the running turn's bot, never an argument. Refusals
    (self-target, unknown bot, hop limit, spawn budget) surface as an error the
    model reads, exactly like a failed delegate.
    """

    source_bot_id = current_bot_id()
    if not source_bot_id:
        raise BotMessageRefused(
            "message_bot has no running bot to speak as: the source bot comes "
            "from the running turn, never from the tool arguments"
        )
    depth = current_hop_depth()
    messenger = BotMessenger(BotRegistry(), conversations=None)
    token = bind_hop_depth(depth + 1)
    try:
        outcome = await messenger.message_bot(source_bot_id, target_bot_id, text, depth=depth)
    finally:
        reset_hop_depth(token)
    return json.dumps(outcome.to_dict(), ensure_ascii=False)


def register_tools(registry: Any) -> int:
    """Register the bot-team tools (idempotent): message, list, delegate."""

    from server.tool_registry import SideEffect

    count = 0
    if not registry.has(TOOL_NAME):
        registry.register(
            TOOL_NAME,
            _TOOL_DESCRIPTION,
            _TOOL_PARAMETERS,
            message_bot,
            max_result_chars=16000,
            # Driving another bot's turn is process work through the same
            # ActionGateway → PermissionEngine boundary every other tool crosses;
            # it is not declared replay-safe, so recovery is manual-only.
            side_effect=SideEffect.PROCESS_EXEC,
            effect_capability="manual_only",
        )
        count += 1
    if not registry.has(LIST_TOOL_NAME):
        registry.register(
            LIST_TOOL_NAME,
            _LIST_TOOL_DESCRIPTION,
            _LIST_TOOL_PARAMETERS,
            list_bots,
            max_result_chars=8000,
            parallel_safe=True,
            side_effect=SideEffect.PURE_READ,
        )
        count += 1
    if not registry.has(DELEGATE_TOOL_NAME):
        registry.register(
            DELEGATE_TOOL_NAME,
            _DELEGATE_TOOL_DESCRIPTION,
            _TOOL_PARAMETERS,
            delegate_bot,
            max_result_chars=8000,
            # Queueing a delegation is process work through the same
            # boundary message_bot crosses; recovery is manual-only.
            side_effect=SideEffect.PROCESS_EXEC,
            effect_capability="manual_only",
        )
        count += 1
    return count


"""Async-delegation tools for tool.py: list_bots + delegate_bot.

Pasted into server/bot_team/tool.py by patch_tools.py (single-quoted
payloads only inside that script; this file is appended verbatim).
"""

LIST_TOOL_NAME = "list_bots"
DELEGATE_TOOL_NAME = "delegate_bot"

_LIST_TOOL_DESCRIPTION = (
    "List the other bots on your team: their bot_id, name, description, "
    "and current status. Read-only. Use it to discover who owns the work "
    "you are being asked about before delegating to them."
)

_DELEGATE_TOOL_DESCRIPTION = (
    "Delegate a request to another bot on your team WITHOUT waiting for "
    "its reply. The request is queued, this call returns a receipt at "
    "once, and the target bot's reply arrives later as a new turn of "
    "yours. Use it when the work can run in the background and you do "
    "not need the answer to finish your own reply. target_bot_id must "
    "be a bot on the team: you cannot delegate to yourself, a bot "
    "outside the team is refused, and a chain of bot messages may not "
    f"be deeper than {MAX_HOP_DEPTH} hops. For an answer you need "
    "before you reply, use message_bot instead."
)

_LIST_TOOL_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
}


async def list_bots() -> str:
    """List the team: id, name, description, status. Read-only."""

    from server.bot_team.attention import AttentionStore, build_board
    from server.bot_team.conversations import ConversationStore

    registry = BotRegistry()
    board = build_board(
        registry,
        conversations=ConversationStore(),
        store=AttentionStore(),
        owner_id=None,
    )
    status_by_id = {
        str(row.get("bot_id")): str(row.get("status")) for row in board
    }
    bots = [
        {
            "bot_id": spec.bot_id,
            "name": spec.name,
            "description": spec.description,
            "status": status_by_id.get(spec.bot_id, "idle"),
        }
        for spec in registry.list()
    ]
    return json.dumps({"bots": bots}, ensure_ascii=False)


async def delegate_bot(target_bot_id: str, text: str) -> str:
    """Queue an async delegation and return its receipt at once.

    The source bot is the running turn's bot, never an argument. Refusals
    (self-target, unknown bot, hop limit) surface as an error the model
    reads, exactly like message_bot.
    """

    source_bot_id = current_bot_id()
    if not source_bot_id:
        raise BotMessageRefused(
            "delegate_bot has no running bot to speak as: the source bot "
            "comes from the running turn, never from the tool arguments"
        )
    depth = current_hop_depth()
    messenger = BotMessenger(BotRegistry(), conversations=None)
    receipt = await messenger.delegate(source_bot_id, target_bot_id, text, depth=depth)
    return json.dumps(receipt, ensure_ascii=False)

__all__ = [
    "TOOL_NAME",
    "bind_hop_depth",
    "current_hop_depth",
    "message_bot",
    "LIST_TOOL_NAME",
    "DELEGATE_TOOL_NAME",
    "list_bots",
    "delegate_bot",
    "register_tools",
    "reset_hop_depth",
]
