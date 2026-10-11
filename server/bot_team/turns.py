"""One bot turn, executed through the existing product path (Lorca P1).

Every bot turn is a real product task: this module only builds the objective,
opens the same Session/Task projection the product shell opens, runs it as that
bot, and records what was said in that bot's conversation. It contains no
scheduler, no task store and no executor of its own — ``_run_product_task``
remains the single entry into ``master_coordinator.execute_structured``.
"""

from __future__ import annotations

import contextlib
import uuid
from dataclasses import dataclass
from typing import Any

from server.bot_team.conversations import ConversationStore, conversation_store
from server.bot_team.runners import LOCAL_RUNNER_ID
from server.goal_run.bot_identity import BotSpec

#: What a bot is told when its turn produced no visible answer. A bot must
#: never look silent: an empty reply is a failure the user can see.
_NO_REPLY = (
    "⚠ {bot} produced no reply for this turn (status={status}). See the task trace for details."
)


@dataclass
class BotTurnResult:
    """Outcome of one bot turn."""

    bot_id: str
    task_id: str
    session_id: str
    reply: str
    status: str
    trace_id: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "bot_id": self.bot_id,
            "task_id": self.task_id,
            "session_id": self.session_id,
            "reply": self.reply,
            "status": self.status,
            "trace_id": self.trace_id,
        }


@dataclass
class BotTurnStart:
    """A turn that is opened but not run yet.

    ``task_id`` is the durable record of the turn from this point on, which is
    what lets a caller answer the user ("accepted, here is the task") before the
    bot has finished thinking.
    """

    bot_id: str
    task_id: str
    session_id: str
    trace_id: str


def build_objective(*, bot: BotSpec, text: str, context: str = "") -> str:
    """Compose the semantic prompt for one turn of ``bot``.

    The bot's one-job description and its instructions are the only place its
    persona can enter the semantic call: ``task.instruction`` is what the
    MasterAgent receives as the user prompt for this GoalRun, so they are
    prepended here instead of inventing a second system prompt.
    """

    parts: list[str] = []
    if bot.instructions.strip():
        parts.append(bot.instructions.strip())
    if bot.description.strip():
        parts.append(f"Your one job: {bot.description.strip()}")
    if context.strip():
        parts.append(context.strip())
    parts.append(str(text or "").strip())
    return "\n\n".join(part for part in parts if part)


def _status_of(response: Any) -> str:
    status = getattr(response, "status", None)
    if status is None and isinstance(response, dict):
        status = response.get("status")
    return str(getattr(status, "value", status) or "unknown")


def extract_reply(response: Any, *, bot: BotSpec) -> str:
    """Pick the user-visible answer out of a canonical execution response."""

    if response is None:
        return _NO_REPLY.format(bot=bot.name or bot.bot_id, status="unknown")
    if isinstance(response, dict):
        summary = response.get("summary") or response.get("final_summary")
        summary = summary or response.get("block_reason")
    else:
        summary = getattr(response, "summary", None) or getattr(response, "final_summary", None)
        summary = summary or getattr(response, "block_reason", None)
    text = str(summary or "").strip()
    if text:
        return text
    return _NO_REPLY.format(bot=bot.name or bot.bot_id, status=_status_of(response))


async def begin_bot_turn(
    *,
    bot: BotSpec,
    text: str,
    user: dict[str, Any],
    context: str = "",
    workspace_id: str | None = None,
    conversations: ConversationStore | None = None,
    trace_id: str | None = None,
) -> BotTurnStart:
    """Open one turn: the product-task projection and the user's message.

    Everything up to here is cheap and local, so a caller can return the task id
    to the user immediately and finish the turn later (see
    :mod:`server.bot_team.async_turns`). Nothing has been executed for the bot.
    """

    from server.routes import product as product_routes

    objective = build_objective(bot=bot, text=text, context=context)
    trace_id = str(trace_id or "").strip() or uuid.uuid4().hex
    session_id, task_id = await product_routes._open_task_projection(
        objective=objective,
        title=f"{(bot.name or bot.bot_id)}: {str(text or '')[:40]}".strip(),
        workspace_id=workspace_id,
        user=user,
        trace_id=trace_id,
    )
    store = conversations if conversations is not None else conversation_store()
    store.append(bot.bot_id, role="user", text=str(text or ""), task_id=task_id)
    return BotTurnStart(
        bot_id=bot.bot_id,
        task_id=task_id,
        session_id=session_id,
        trace_id=trace_id,
    )


async def finish_bot_turn(
    *,
    bot: BotSpec,
    start: BotTurnStart,
    text: str,
    user: dict[str, Any],
    context: str = "",
    workspace_id: str | None = None,
    conversations: ConversationStore | None = None,
) -> BotTurnResult:
    """Run the turn opened by :func:`begin_bot_turn` and record the reply."""

    from server.routes import product as product_routes

    store = conversations if conversations is not None else conversation_store()
    objective = build_objective(bot=bot, text=text, context=context)
    response = await product_routes._run_product_task(
        task_id=start.task_id,
        session_id=start.session_id,
        objective=objective,
        provider=bot.provider or None,
        model=bot.model or None,
        config={},
        user=dict(user),
        project_root=workspace_id or "/repo",
        bot_id=bot.bot_id,
    )
    reply = extract_reply(response, bot=bot)
    store.append(bot.bot_id, role="bot", text=reply, task_id=start.task_id)
    # NANO-W10 group B turn-end hooks: daily memory log, memory review, context
    # steward, proactive-compaction gate.  Every hook is flag-gated and wrapped,
    # so with the default flags this is a no-op that cannot fail the turn.
    with contextlib.suppress(Exception):
        from server.bot_team.turn_end import turn_end_hooks

        await turn_end_hooks(
            bot_id=bot.bot_id,
            reply=reply,
            status=_status_of(response),
            session_id=start.session_id,
        )
    return BotTurnResult(
        bot_id=bot.bot_id,
        task_id=start.task_id,
        session_id=start.session_id,
        reply=reply,
        status=_status_of(response),
        trace_id=start.trace_id,
    )


async def run_bot_turn(
    *,
    bot: BotSpec,
    text: str,
    user: dict[str, Any],
    context: str = "",
    workspace_id: str | None = None,
    conversations: ConversationStore | None = None,
    trace_id: str | None = None,
) -> BotTurnResult:
    """Run one turn of ``bot`` and record both sides in its conversation.

    The request is awaited, so the caller only returns once the bot replied (or
    its turn failed visibly). The returned :class:`BotTurnResult` carries the
    product task id, which is also the durable record of the turn.

    Lorca (P6): when ``bot.runner_id`` names another machine, the turn is
    forwarded there instead of executed here. Everything a caller needs to know
    stays in this one entry point.
    """

    # NANO-W6: an explicit @runner mention routes the turn to that runner,
    # regardless of where the bot is bound. Unknown names get a helpful error
    # listing the registered runners — never a silent fallback to local.
    from server.bot_team.mention import route_text_to_runner

    mention = route_text_to_runner(text)
    if mention is not None and "runner_id" in mention:
        # Imported lazily: the client imports this module for its result type.
        from server.bot_team.runner_client import forward_bot_turn

        return await forward_bot_turn(
            bot=bot,
            text=mention["rest"] or text,
            user=user,
            context=context,
            workspace_id=workspace_id,
            trace_id=trace_id,
            conversations=conversations,
        )
    if mention is not None and mention.get("error"):
        names = ", ".join(
            str(view.get("name") or view.get("runner_id")) for view in mention.get("runners", [])
        )
        reply = (
            f"找不到 runner {mention['error'].replace('unknown runner ', '')}。"
            f"已注册的 runner：{names or '（无）'}。"
            f"请用 @<runner-name> 重试，例如 @{names.split(', ')[0].split(' ')[0] if names else 'runner'} <消息>。"
        )
        return BotTurnResult(
            bot_id=bot.bot_id,
            task_id="",
            session_id="",
            reply=reply,
            status="error",
            trace_id=trace_id or "",
        )

    runner_id = (bot.runner_id or LOCAL_RUNNER_ID).strip() or LOCAL_RUNNER_ID
    if runner_id != LOCAL_RUNNER_ID:
        # Imported lazily: the client imports this module for its result type.
        from server.bot_team.runner_client import forward_bot_turn

        return await forward_bot_turn(
            bot=bot,
            text=text,
            user=user,
            context=context,
            workspace_id=workspace_id,
            trace_id=trace_id,
            conversations=conversations,
        )

    start = await begin_bot_turn(
        bot=bot,
        text=text,
        trace_id=trace_id,
        user=user,
        context=context,
        workspace_id=workspace_id,
        conversations=conversations,
    )
    return await finish_bot_turn(
        bot=bot,
        start=start,
        text=text,
        user=user,
        context=context,
        workspace_id=workspace_id,
        conversations=conversations,
    )


__all__ = [
    "BotTurnResult",
    "BotTurnStart",
    "begin_bot_turn",
    "build_objective",
    "extract_reply",
    "finish_bot_turn",
    "run_bot_turn",
]
