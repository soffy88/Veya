"""Bot → bot messaging on top of the existing delegation runtime (Lorca P2).

``message_bot(source, target, text)`` is the only way one bot asks another for
work. It is a thin driver over the runtime Veya already had:

* :class:`~runtime.execution.delegate_runtime.DelegateRuntime.run_cross_bot`
  owns admission, timeout, cancellation and the explicit cross-bot provenance
  (source/target/goal-run refs);
* :class:`~runtime.execution.spawn_guard.SpawnGuard` owns the deterministic
  limits: ``max_depth=3`` hop limit and one per-turn token budget.

The target's turn is an ordinary product task of the target bot
(:mod:`server.bot_team.turns`), so no second scheduler, task store or executor
is introduced here. Messages are refused (not repaired) for a self-target, an
unknown bot, or a hop chain deeper than :data:`MAX_HOP_DEPTH`.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from runtime.execution.delegate_runtime import DelegateRuntime
from runtime.execution.models import DelegateRequest
from runtime.execution.spawn_guard import SpawnBudget, SpawnGuard
from server.bot_team.conversations import ConversationStore
from server.bot_team.turns import run_bot_turn
from server.goal_run.bot_identity import BotRegistry, BotSpec

#: Hop limit for one conversation: source → target → … at most this deep.
MAX_HOP_DEPTH = 3

#: Per-turn token budget of the guard below. It is per call, so one bot
#: conversation can never spend another turn's budget.
MAX_MESSAGE_TOKENS = 300_000


class BotMessageRefused(RuntimeError):
    """A bot message was refused (self-target, unknown bot, or hop limit)."""


@dataclass
class BotMessageOutcome:
    """What one bot message produced."""

    source_bot_id: str
    target_bot_id: str
    delegate_id: str
    depth: int
    status: str
    reply: str
    target_task_id: str
    target_session_id: str
    goal_run_id: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_bot_id": self.source_bot_id,
            "target_bot_id": self.target_bot_id,
            "delegate_id": self.delegate_id,
            "depth": self.depth,
            "status": self.status,
            "reply": self.reply,
            "target_task_id": self.target_task_id,
            "target_session_id": self.target_session_id,
            "goal_run_id": self.goal_run_id,
        }


def default_guard() -> SpawnGuard:
    """The deterministic limits one bot message runs under."""

    return SpawnGuard(SpawnBudget(max_depth=MAX_HOP_DEPTH, max_tokens=MAX_MESSAGE_TOKENS))


class BotMessenger:
    """Service that lets one bot message another."""

    def __init__(
        self,
        registry: BotRegistry,
        *,
        conversations: ConversationStore | None = None,
        guard: SpawnGuard | None = None,
    ):
        self.registry = registry
        self.conversations = conversations if conversations is not None else ConversationStore()
        self._guard = guard

    def _guard_for_turn(self) -> SpawnGuard:
        return self._guard if self._guard is not None else default_guard()

    def _bot_or_refuse(self, bot_id: str) -> BotSpec:
        bot = self.registry.get(str(bot_id or ""))
        if bot is None:
            raise BotMessageRefused(f"unknown bot: {bot_id!r}")
        return bot

    async def message_bot(
        self,
        source_bot_id: str,
        target_bot_id: str,
        text: str,
        *,
        user: dict[str, Any] | None = None,
        depth: int = 0,
        workspace_id: str | None = None,
    ) -> BotMessageOutcome:
        """Ask ``target_bot_id`` for work on behalf of ``source_bot_id``.

        The target runs a normal turn; its reply is returned to the caller and
        recorded in both conversations (the target's own, and a note in the
        source's). ``depth`` is the hop count of the chain that led here and
        must stay below :data:`MAX_HOP_DEPTH`.
        """

        source = self._bot_or_refuse(source_bot_id)
        target = self._bot_or_refuse(target_bot_id)
        if source.bot_id == target.bot_id:
            raise BotMessageRefused(f"bot {source.bot_id!r} cannot message itself")
        if depth < 0:
            raise BotMessageRefused("bot message depth must be non-negative")
        if depth >= MAX_HOP_DEPTH:
            raise BotMessageRefused(
                f"bot message hop limit reached: depth={depth}, max={MAX_HOP_DEPTH}"
            )

        delegate_id = f"botmsg_{uuid.uuid4().hex[:16]}"
        # Routing refs, not new state: the durable execution of the target's
        # turn is its own GoalRun, created by the product path below.
        parent_trace_id = f"bot:{source.bot_id}:{delegate_id}"
        target_goal_run_id = f"bot-turn:{target.bot_id}:{delegate_id}"
        request = DelegateRequest(
            delegate_id=delegate_id,
            parent_task_id=delegate_id,
            parent_trace_id=parent_trace_id,
            objective=str(text or ""),
            source_bot_id=source.bot_id,
            target_bot_id=target.bot_id,
            target_goal_run_id=target_goal_run_id,
            bot_id=source.bot_id,
            depth=depth,
        )
        guard = self._guard_for_turn()
        runtime = DelegateRuntime(guard, goal_run_id=parent_trace_id, bot_id=source.bot_id)
        caller = dict(user or {"user_id": "bot-message"})
        captured: dict[str, Any] = {}

        async def target_turn(_cancel: Any) -> dict[str, Any]:
            """Bot B's semantic work: one normal turn of the target bot."""

            turn = await run_bot_turn(
                bot=target,
                text=str(text or ""),
                context=(
                    f"Bot {source.bot_id!r} sent you this message. "
                    "Answer it as your one job, then stop."
                ),
                user=caller,
                workspace_id=workspace_id,
                conversations=self.conversations,
            )
            captured["turn"] = turn
            return {
                "status": "complete",
                "stop_reason": "completed",
                "summary": turn.reply,
                "goal_run_id": target_goal_run_id,
                "evidence_refs": [f"task:{turn.task_id}"],
            }

        result = await runtime.run_cross_bot(
            request,
            target_turn,
            source_bot_id=source.bot_id,
            target_bot_id=target.bot_id,
            target_goal_run_id=target_goal_run_id,
        )
        turn = captured.get("turn")
        reply = str(result.summary or "").strip()
        if not reply:
            # Never silent: a failed delegate is visible in both conversations.
            reply = f"⚠ bot {target.bot_id!r} could not answer (delegate status={result.status})."
        target_task_id = str(getattr(turn, "task_id", "") or f"task:{result.delegate_id}")
        # The source bot's conversation keeps what came back, so a human
        # reading either side sees the same exchange.
        self.conversations.append(
            source.bot_id,
            role="bot",
            text=reply,
            task_id=target_task_id,
        )
        return BotMessageOutcome(
            source_bot_id=source.bot_id,
            target_bot_id=target.bot_id,
            delegate_id=result.delegate_id,
            depth=depth,
            status=str(result.status),
            reply=reply,
            target_task_id=target_task_id,
            target_session_id=str(getattr(turn, "session_id", "") or ""),
            goal_run_id=str(result.goal_run_id or ""),
        )


def messenger(
    registry: BotRegistry,
    *,
    conversations: ConversationStore | None = None,
    guard: SpawnGuard | None = None,
) -> BotMessenger:
    return BotMessenger(registry, conversations=conversations, guard=guard)


async def message_bot(
    source_bot_id: str,
    target_bot_id: str,
    text: str,
    *,
    registry: BotRegistry,
    user: dict[str, Any] | None = None,
    depth: int = 0,
    conversations: ConversationStore | None = None,
    workspace_id: str | None = None,
) -> BotMessageOutcome:
    """Module-level convenience wrapper around :class:`BotMessenger`."""

    return await BotMessenger(registry, conversations=conversations).message_bot(
        source_bot_id,
        target_bot_id,
        text,
        user=user,
        depth=depth,
        workspace_id=workspace_id,
    )


__all__ = [
    "MAX_HOP_DEPTH",
    "MAX_MESSAGE_TOKENS",
    "BotMessageOutcome",
    "BotMessageRefused",
    "BotMessenger",
    "default_guard",
    "message_bot",
]
