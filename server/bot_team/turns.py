"""One bot turn, executed through the existing product path (Lorca P1).

Every bot turn is a real product task: this module only builds the objective,
opens the same Session/Task projection the product shell opens, runs it as that
bot, and records what was said in that bot's conversation. It contains no
scheduler, no task store and no executor of its own — ``_run_product_task``
remains the single entry into ``master_coordinator.execute_structured``.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from server.bot_team.conversations import ConversationStore, conversation_store
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


async def run_bot_turn(
    *,
    bot: BotSpec,
    text: str,
    user: dict[str, Any],
    context: str = "",
    workspace_id: str | None = None,
    conversations: ConversationStore | None = None,
) -> BotTurnResult:
    """Run one turn of ``bot`` and record both sides in its conversation.

    The request is awaited, so the caller only returns once the bot replied (or
    its turn failed visibly). The returned :class:`BotTurnResult` carries the
    product task id, which is also the durable record of the turn.
    """

    # Imported lazily: the route module pulls the product stores, and this
    # package must stay importable from the runtime/durable layers too.
    from server.routes import product as product_routes

    store = conversations if conversations is not None else conversation_store()
    objective = build_objective(bot=bot, text=text, context=context)
    trace_id = uuid.uuid4().hex
    session_id, task_id = await product_routes._open_task_projection(
        objective=objective,
        title=f"{(bot.name or bot.bot_id)}: {str(text or '')[:40]}".strip(),
        workspace_id=workspace_id,
        user=user,
        trace_id=trace_id,
    )
    store.append(bot.bot_id, role="user", text=str(text or ""), task_id=task_id)

    response = await product_routes._run_product_task(
        task_id=task_id,
        session_id=session_id,
        objective=objective,
        provider=bot.provider or None,
        model=bot.model or None,
        config={},
        user=dict(user),
        project_root=workspace_id or "/repo",
        bot_id=bot.bot_id,
    )
    reply = extract_reply(response, bot=bot)
    store.append(bot.bot_id, role="bot", text=reply, task_id=task_id)
    return BotTurnResult(
        bot_id=bot.bot_id,
        task_id=task_id,
        session_id=session_id,
        reply=reply,
        status=_status_of(response),
        trace_id=trace_id,
    )


__all__ = ["BotTurnResult", "build_objective", "extract_reply", "run_bot_turn"]
