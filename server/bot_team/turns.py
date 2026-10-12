"""One bot turn, executed through the existing product path (Lorca P1).

Every bot turn is a real product task: this module only builds the objective,
opens the same Session/Task projection the product shell opens, runs it as that
bot, and records what was said in that bot's conversation. It contains no
scheduler, no task store and no executor of its own — ``_run_product_task``
remains the single entry into ``master_coordinator.execute_structured``.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
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


#: Raw internal strings that must never become a bot's reply (W11-C / S12).
#: They are diagnostics: they stay in the task record and the event log.
_RAW_MARKERS = (
    "Missing required evidence",
    "ac-canonical_action_observed",
    "Traceback (most recent call last)",
    'File "',
)


def _friendly_reason(raw: Any) -> str:
    """A friendly Chinese sentence for one raw failure reason."""

    text = str(raw or "").strip()
    lowered = text.lower()
    if not text:
        return "这一步没有完成，我没有改动任何文件。你可以让我换一种方式再试。"
    if any(marker in text for marker in _RAW_MARKERS):
        return "这一步我没能拿到可以确认完成的证据，所以先停在这里，没有改动你的文件。"
    if "approval" in lowered or "notauthorized" in lowered or "not authorized" in lowered:
        return "这个操作需要你批准后才能执行。批准列表里有一条待处理的申请，通过后我会继续。"
    if "timed out" in lowered or "timeout" in lowered:
        return "等待审批超时了，操作没有执行。需要的话你可以在批准列表里重新发起。"
    if text.startswith("Goal:"):
        return "这一步没有完成，我没有改动任何文件。你可以让我换一种方式再试。"
    return "这一步没有完成：" + text[:120]


def _sanitised_summary(response: Any) -> str:
    """Pick the summary/block reason and reword it for the user (S12)."""

    if isinstance(response, dict):
        summary = response.get("summary") or response.get("final_summary")
        summary = summary or response.get("block_reason")
    else:
        summary = getattr(response, "summary", None) or getattr(response, "final_summary", None)
        summary = summary or getattr(response, "block_reason", None)
    text = str(summary or "").strip()
    # A finished turn's own summary is the answer; only reword what is raw
    # diagnostics or a failure.
    if (
        text
        and _status_of(response) in ("completed", "partial_completed")
        and not any(marker in text for marker in _RAW_MARKERS)
        and not text.startswith("Goal:")
    ):
        return text
    return _friendly_reason(summary)


def extract_reply(response: Any, *, bot: BotSpec) -> str:
    """Pick the user-visible answer out of a canonical execution response.

    W11-C (S12): whatever went wrong, the user sees a clear friendly Chinese
    sentence.  A raw verifier reason ("Missing required evidence: …"), a
    criterion id ("ac-canonical_action_observed"), a traceback or the GoalRun
    report card ("Goal: … Status: … Tasks: …") is never shown.  The raw reason
    stays in the task record and the event log, which is where an operator
    looks for it.
    """

    if response is None:
        return _NO_REPLY.format(bot=bot.name or bot.bot_id, status="unknown")
    # W11: a completed conversational turn carries the model's own answer;
    # show that, not the GoalRun report ("Goal: … Status: … Tasks: …").
    direct = (
        response.get("reply") if isinstance(response, dict) else getattr(response, "reply", None)
    )
    if str(direct or "").strip() and _status_of(response) in ("completed", "partial_completed"):
        return str(direct).strip()
    text = _sanitised_summary(response)
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
    try:
        from server.feature_flags import enabled as _flag_enabled

        if _flag_enabled("VEYA_BOT_SECRETS"):
            from server.bot_team.secrets import bot_secret_store
            from veya.oskill.pure.secret_masking import mask_secret_values

            mask_map = bot_secret_store().get_masking_map()
            if mask_map:
                reply = mask_secret_values(reply, mask_map)
    except Exception:
        pass
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
    finishing = finish_bot_turn(
        bot=bot,
        start=start,
        text=text,
        user=user,
        context=context,
        workspace_id=workspace_id,
        conversations=conversations,
    )
    if not _approval_gates_on():
        return await finishing
    return await _finish_or_await_approval(
        finishing, bot=bot, start=start, user=user, conversations=conversations
    )


#: Friendly reply while a side-effect action waits on the user's approval.
APPROVAL_WAIT_REPLY = (
    "这个操作需要你批准后才会执行。我已经在批准列表里提交了申请，"
    "批准后我会继续，结果会发在这里。"
)

#: Turns still running after their caller got the "waiting for approval" reply.
_BACKGROUND_TURNS: set[Any] = set()


def _approval_gates_on() -> bool:
    try:
        from server.approvals import approval_gates_enabled

        return bool(approval_gates_enabled())
    except Exception:
        return False


def _new_pending_approvals(user: dict[str, Any], since: float) -> list[dict[str, Any]]:
    """Pending approvals the user's scope gained since ``since`` (epoch secs)."""

    from server.approvals import get_approval_service

    service = get_approval_service()
    scope = service.scope_for(str((user or {}).get("user_id") or "anonymous"))
    rows = service.list_pending(scope=scope)
    return [row for row in rows if float(row.get("created_at") or 0) >= since]


async def _finish_or_await_approval(
    finishing: Any,
    *,
    bot: BotSpec,
    start: BotTurnStart,
    user: dict[str, Any],
    conversations: ConversationStore | None,
) -> BotTurnResult:
    """W11-C (S11): never hold a chat request hostage to a human approval.

    The side-effect action itself is gated by the existing approval authority
    (``server.user_control`` -> ``ApprovalService``), which pauses the turn
    until the user decides.  The chat caller should not sit on an open request
    for that: as soon as the turn opens a pending approval we answer with a
    friendly "waiting for your approval" reply.  The turn keeps running in the
    background, performs the action once approved, and records its final
    reply in the conversation like any other turn.
    """

    since = time.time() - 1.0
    task = asyncio.ensure_future(finishing)
    while not task.done():
        done, _ = await asyncio.wait({task}, timeout=0.5)
        if done:
            break
        try:
            pending = _new_pending_approvals(user, since)
        except Exception:
            pending = []
        if pending:
            _BACKGROUND_TURNS.add(task)
            task.add_done_callback(_BACKGROUND_TURNS.discard)
            store = conversations if conversations is not None else conversation_store()
            store.append(bot.bot_id, role="bot", text=APPROVAL_WAIT_REPLY, task_id=start.task_id)
            return BotTurnResult(
                bot_id=bot.bot_id,
                task_id=start.task_id,
                session_id=start.session_id,
                reply=APPROVAL_WAIT_REPLY,
                status="awaiting_approval",
                trace_id=start.trace_id,
            )
    return task.result()


__all__ = [
    "BotTurnResult",
    "BotTurnStart",
    "begin_bot_turn",
    "build_objective",
    "extract_reply",
    "finish_bot_turn",
    "run_bot_turn",
]
