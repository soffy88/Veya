"""Opt-in async bot turns (Lorca P5).

A bot turn can take minutes. Holding an HTTP request open for minutes is what
makes a phone browser drop the tab, so the bot and group message endpoints
accept ``?async=1``: they open the turn — which is where the task id comes
from — answer ``202`` with that id, and finish the turn in the background.

This is a deferral, not a second execution path. The background coroutine calls
exactly the same :func:`begin_bot_turn` / :func:`finish_bot_turn` (or
``run_group_turn``) the synchronous endpoints call, which is the same product
path and the same GoalRun. There is no scheduler, no new task store and no new
executor here: the durable record of a deferred turn is still the product Task,
which is why the 202 carries ``task_id`` and the existing GET endpoints return
the result once it lands.

What is new is only bookkeeping for *this process*: which background turns are
still in flight. That lives in memory, so a restart forgets it — and that is
safe, because the turn itself is recorded in the durable stores (the async
answer is not). The modules make no claim of surviving a restart.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

from server.bot_team.chef import record_team_proposal
from server.bot_team.conversations import ConversationStore
from server.bot_team.groups import GroupChat, run_group_turn
from server.bot_team.push import announce_bot_reply, announce_group_round
from server.bot_team.turns import (
    BotTurnResult,
    begin_bot_turn,
    finish_bot_turn,
)
from server.goal_run.bot_identity import BotRegistry, BotSpec

_logg = logging.getLogger("veya.bot_team.async_turns")


@dataclass
class AsyncOutcome:
    """The finished half of a deferred turn, including Chef's proposal."""

    result: BotTurnResult
    proposal: dict[str, Any] | None = None


@dataclass
class AsyncTurnRef:
    """What ``202`` hands back: the handle a client can poll or wait for."""

    task_id: str
    session_id: str
    bot_id: str
    kind: str
    group_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "status": "accepted",
            "task_id": self.task_id,
            "session_id": self.session_id,
            "bot_id": self.bot_id,
            "kind": self.kind,
        }
        if self.kind == "group_turn":
            payload["group_id"] = self.group_id
            payload["result_url"] = f"/api/v1/bots/groups/{self.group_id}/messages"
        else:
            payload["result_url"] = f"/api/v1/bots/{self.bot_id}/messages"
            payload["workbench_url"] = f"/workbench/{self.task_id}"
        return payload


@dataclass
class _Pending:
    ref: AsyncTurnRef
    task: asyncio.Task[Any]


#: Deferred turns still running in this process. Not a store (see module docstring).
_pending: dict[str, _Pending] = {}
#: Strong references so the event loop will not garbage-collect a live turn.
_tasks: set[asyncio.Task[Any]] = set()


def pending_refs() -> list[AsyncTurnRef]:
    """Deferred turns still running in this process."""

    return [item.ref for item in _pending.values()]


async def start_bot_turn_async(
    *,
    bot: BotSpec,
    text: str,
    user: dict[str, Any],
    workspace_id: str | None = None,
    conversations: ConversationStore | None = None,
) -> AsyncTurnRef:
    """Open a bot turn, answer with its id, and finish it in the background."""

    start = await begin_bot_turn(
        bot=bot,
        text=text,
        user=user,
        workspace_id=workspace_id,
        conversations=conversations,
    )
    ref = AsyncTurnRef(
        task_id=start.task_id,
        session_id=start.session_id,
        bot_id=bot.bot_id,
        kind="bot_turn",
    )
    _spawn(
        ref,
        _run_bot_turn(
            ref=ref,
            bot=bot,
            start=start,
            text=text,
            user=user,
            workspace_id=workspace_id,
            conversations=conversations,
        ),
    )
    return ref


async def start_group_turn_async(
    *,
    group: GroupChat,
    text: str,
    user: dict[str, Any],
    registry: BotRegistry,
    workspace_id: str | None = None,
    conversations: ConversationStore | None = None,
) -> AsyncTurnRef:
    """Offer the group a round, answer now, finish it in the background."""
    store = conversations if conversations is not None else ConversationStore()
    ref = AsyncTurnRef(
        task_id=f"group-turn:{group.id}",
        session_id="",
        bot_id="",
        kind="group_turn",
        group_id=group.id,
    )
    _spawn(
        ref,
        _run_group_turn(
            ref=ref,
            group=group,
            text=text,
            user=user,
            registry=registry,
            workspace_id=workspace_id,
            conversations=store,
        ),
    )
    return ref


def _spawn(ref: AsyncTurnRef, coro: Any) -> None:
    task = asyncio.ensure_future(coro)
    _tasks.add(task)
    _pending[ref.task_id] = _Pending(ref=ref, task=task)
    task.add_done_callback(lambda finished: _forget(ref.task_id, finished))


def _forget(task_id: str, task: asyncio.Task[Any]) -> None:
    _tasks.discard(task)
    _pending.pop(task_id, None)


async def _run_bot_turn(
    *,
    ref: AsyncTurnRef,
    bot: BotSpec,
    start: Any,
    text: str,
    user: dict[str, Any],
    workspace_id: str | None,
    conversations: ConversationStore | None,
) -> AsyncOutcome:
    """Finish a deferred bot turn and announce it once it is on record."""

    turn = await finish_bot_turn(
        bot=bot,
        start=start,
        text=text,
        user=user,
        workspace_id=workspace_id,
        conversations=conversations,
    )
    # A Chef turn still produces a confirmable proposal when its turn was
    # answered asynchronously (same recording the sync endpoint does).
    proposal = record_team_proposal(
        bot_id=turn.bot_id,
        text=turn.reply,
        task_id=turn.task_id,
        owner_id=str(user.get("user_id") or ""),
    )
    announce_bot_reply(
        bot_name=bot.name or bot.bot_id,
        reply=turn.reply,
        task_id=turn.task_id,
        user_id=str(user.get("user_id") or ""),
        bot_id=turn.bot_id,
    )
    return AsyncOutcome(result=turn, proposal=proposal)


async def _run_group_turn(
    *,
    ref: AsyncTurnRef,
    group: GroupChat,
    text: str,
    user: dict[str, Any],
    registry: BotRegistry,
    workspace_id: str | None,
    conversations: ConversationStore,
) -> dict[str, Any]:
    """Finish a deferred group round and announce who spoke first."""

    result = await run_group_turn(
        group=group,
        text=text,
        user=user,
        registry=registry,
        conversations=conversations,
        workspace_id=workspace_id,
    )
    announce_group_round(
        group_name=group.name,
        group_id=group.id,
        replies=result.get("replies") or [],
        user_id=str(user.get("user_id") or ""),
    )
    return result


async def drain_async_turns(timeout: float = 5.0) -> dict[str, Any]:
    """Await every deferred turn still running in this process.

    The deterministic hook tests use: without it a background turn may not have
    finished when the assertion runs, and a flaky test is a broken test.
    """

    tasks = [item.task for item in list(_pending.values())]
    if not tasks:
        return {"drained": 0, "failed": 0}
    done, _pending_set = await asyncio.wait(tasks, timeout=timeout)
    failed = 0
    for task in done:
        if task.cancelled() or task.exception() is not None:
            failed += 1
            _logg.warning("deferred bot turn failed", exc_info=task.exception())
    return {"drained": len(done), "failed": failed}


__all__ = [
    "AsyncTurnRef",
    "drain_async_turns",
    "pending_refs",
    "start_bot_turn_async",
    "start_group_turn_async",
]
