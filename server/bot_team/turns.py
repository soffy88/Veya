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


def _turn_queue() -> Any:
    """The process turn queue, or ``None`` when ``VEYA_TURN_QUEUE`` is off.

    The queue is the one authority for "is this conversation running", so a
    turn marks itself there for its whole duration — that is what lets a
    mid-turn correction (W11-E S4) reach it.
    """

    from server.nano_w10.turn_queue import get_turn_queue, turn_queue_enabled

    return get_turn_queue() if turn_queue_enabled() else None


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
    if getattr(bot, "available_skill_ids", None):
        import os

        from server.skill_progressive import is_skill_progressive_enabled, render_skills_block

        if is_skill_progressive_enabled():
            skills_block = render_skills_block(bot.available_skill_ids, progressive=True)
            if skills_block:
                parts.append(skills_block)
        elif os.environ.get("VEYA_SKILL_INJECT_FULL", "0").lower() in ("1", "true", "yes"):
            skills_block = render_skills_block(bot.available_skill_ids, progressive=False)
            if skills_block:
                parts.append(skills_block)
    if getattr(bot, "bot_id", None):
        from server.feature_flags import enabled

        if enabled("VEYA_MEMORY_UI"):
            from server.bot_team.memory import BotMemoryStore

            ug = BotMemoryStore(bot.bot_id).read("USER_GLOBAL.md")
            if ug.strip():
                parts.append(
                    f"<!-- user-global -->\n### User Global Background (Read-Only)\n\n{ug.strip()}"
                )

    # W16: Inject project brief block and today's log tail if project brain enabled
    try:
        from server.feature_flags import enabled as _flag_enabled

        if _flag_enabled("VEYA_PROJECT_BRAIN"):
            proj_root = getattr(bot, "workspace_path", "") or getattr(
                bot, "default_workspace_id", ""
            )
            if proj_root:
                from pathlib import Path

                p = Path(proj_root)
                if (p / ".veya-project").is_dir():
                    from server.project_brain.brief import load_brief, render_brief
                    from server.project_store import ProjectStore

                    brief = load_brief(p)
                    brief_md = render_brief(brief).strip()
                    parts.append(
                        f"<!-- veya:project-brief -->\n{brief_md}\n<!-- /veya:project-brief -->"
                    )

                    today_log = ProjectStore(p).read_log()
                    if today_log.strip():
                        tail_lines = today_log.strip().splitlines()[-20:]
                        log_tail = "\n".join(tail_lines)
                        parts.append(
                            f"<!-- veya:project-log-tail -->\n{log_tail}\n<!-- /veya:project-log-tail -->"
                        )
    except Exception:
        pass

    if context.strip():
        parts.append(context.strip())
    parts.append("请使用中文（与用户消息或回忆的语言保持一致）简洁、准确地回答用户。")
    parts.append(str(text or "").strip())
    return "\n\n".join(part for part in parts if part)


# ---------------------------------------------------------------------------
# W12-A2: plain phrasing → supervise_task
# ---------------------------------------------------------------------------

# Matches "每N分钟告诉我进度" / "每N分钟汇报" / "每N分钟告诉我进度" etc.
_SUPERVISOR_PHRASE_RE = __import__("re").compile(
    r"(?:每|每隔)?\s*(\d+)\s*(?:分钟|min|分钟一次|分钟)"
    r"(?:告诉|汇报|上报|报告|给我)?"
    r"(?:进度|进展|情况|状态)?"
)


def parse_supervisor_phrase(text: str) -> dict[str, Any] | None:
    """Parse plain phrasing like "每30分钟告诉我进度" into supervisor params.

    Returns a dict with `report_every_minutes` (and optionally
    `stall_after_minutes`) when the text contains a reporting cadence,
    else ``None``. Pure pattern match — no side effects, no model call.
    """
    raw = str(text or "")
    if not raw:
        return None
    m = _SUPERVISOR_PHRASE_RE.search(raw)
    if not m:
        return None
    minutes = int(m.group(1))
    if minutes <= 0:
        return None
    return {
        "report_every_minutes": minutes,
        "stall_after_minutes": max(1, minutes // 2),
    }


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
    "this is a shim response",
    "兜底失败",
)


#: The reply when no model answered at all (W11 S12 follow-up).
_MODEL_UNAVAILABLE_REPLY = "模型服务暂时没有响应，这条消息没有处理。请稍后再发一次。"


def _friendly_reason(raw: Any) -> str:
    """A friendly Chinese sentence for one raw failure reason."""

    text = str(raw or "").strip()
    lowered = text.lower()
    if not text:
        return "这一步没有完成，我没有改动任何文件。你可以让我换一种方式再试。"
    if "this is a shim response" in text or "兜底失败" in text:
        return _MODEL_UNAVAILABLE_REPLY
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
    dstr = str(direct or "").strip()
    # Check if direct response is a raw tool output / tool receipt / memory update receipt
    is_raw_tool_output = (
        dstr.startswith("[Channel:")
        or "EXTERNAL DATA:" in dstr
        or "✅ 永久记忆已更新" in dstr
        or "Source URL:" in dstr
    )
    if (
        dstr
        and not is_raw_tool_output
        and _status_of(response) in ("completed", "partial_completed")
        and not any(marker in dstr for marker in _RAW_MARKERS)
    ):
        return dstr
    if is_raw_tool_output:
        return "这一步操作已完成（工具已返回结果或更新了记忆），但模型未能生成总结回复。请继续您的提问或指令。"
    if any(marker in dstr for marker in ("this is a shim response", "兜底失败")):
        # No model answered (every provider failed or none is configured): say
        # so plainly instead of showing the router's placeholder text.
        return _MODEL_UNAVAILABLE_REPLY
    text = _sanitised_summary(response)
    if text and not (
        text.startswith("[Channel:") or "EXTERNAL DATA:" in text or "✅ 永久记忆已更新" in text
    ):
        return text
    return _MODEL_UNAVAILABLE_REPLY


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
    # W11-E S4: the turn is now in flight. Marking the conversation running is
    # what lets a correction typed mid-turn reach this turn instead of 409ing.
    queue = _turn_queue()
    if queue is not None:
        queue.mark_running(bot.bot_id)
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
    reply_meta: dict[str, Any] | None = None,
) -> BotTurnResult:
    """Run the turn opened by :func:`begin_bot_turn` and record the reply.

    ``reply_meta`` tags the bot's reply (a routine turn marks it
    ``{"source": "routine", "routine_id": …}`` so the UI can tell it apart).
    """

    from server.routes import product as product_routes

    store = conversations if conversations is not None else conversation_store()
    queue = _turn_queue()
    if queue is not None:
        # W11 S3: people send a thought in pieces.  Hold the first model call a
        # moment so messages typed right after this one join the same answer,
        # instead of one model run per piece (each piece cost a full run).
        await asyncio.sleep(_coalesce_window_s())
        burst = [str(t).strip() for t in queue.drain_steers(bot.bot_id) if str(t or "").strip()]
        if burst:
            text = "\n".join([str(text or ""), *burst])
    objective = build_objective(bot=bot, text=text, context=context)
    try:
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
        # W11-E S4: a correction that arrived after the model's last call was
        # not seen by it.  Answer again with the correction in view, inside the
        # same turn, so the reply the user gets follows what they asked last.
        rounds = 0
        while queue is not None and rounds < 2:
            late = queue.drain_steers(bot.bot_id)
            late = [str(t).strip() for t in late if str(t or "").strip()]
            if not late:
                break
            rounds += 1
            corrected = (
                f"{text}\n\n用户在你回答过程中又发来了消息（一起回答；与前面冲突时以新消息为准）："
                + "；".join(late)
            )
            response = await product_routes._run_product_task(
                task_id=start.task_id,
                session_id=start.session_id,
                objective=build_objective(bot=bot, text=corrected, context=context),
                provider=bot.provider or None,
                model=bot.model or None,
                config={},
                user=dict(user),
                project_root=workspace_id or "/repo",
                bot_id=bot.bot_id,
            )
    finally:
        # W11-E S4: the turn is over (or failed) — release the conversation so
        # the next correction is not steered into a turn that no longer runs.
        if queue is not None:
            queue.mark_finished(bot.bot_id)
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
    store.append(bot.bot_id, role="bot", text=reply, task_id=start.task_id, meta=reply_meta)
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
    reply_meta: dict[str, Any] | None = None,
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

    # W12-A2: "每N分钟告诉我进度" style phrasing becomes a supervise_task
    # call instead of a model turn. The model never has to decide it on its
    # own — the phrase parser turns the user's sentence into the tool call.
    supervisor_start = await _supervise_from_phrase(
        bot=bot,
        text=text,
        user=user,
        workspace_id=workspace_id,
        conversations=conversations,
    )
    if supervisor_start is not None:
        return supervisor_start

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
        reply_meta=reply_meta,
    )
    if not _approval_gates_on():
        return await finishing
    return await _finish_or_await_approval(
        finishing, bot=bot, start=start, user=user, conversations=conversations
    )


async def _supervise_from_phrase(
    *,
    bot: BotSpec,
    text: str,
    user: dict[str, Any],
    workspace_id: str | None,
    conversations: ConversationStore | None,
) -> BotTurnResult | None:
    """If the user's text matches a W12-A2 supervisor phrase, start a
    supervised long task and return the resulting BotTurnResult. Else None."""
    from server.supervisor import enabled as is_supervisor_enabled

    if not is_supervisor_enabled():
        return None

    parsed = parse_supervisor_phrase(text)
    if parsed is None:
        return None

    project_root = str(workspace_id or "")
    if not project_root:
        # No workspace bound: never fall back to the process cwd (/app is
        # read-only in prod). Use the bot's own dir under the write root.
        import os
        from pathlib import Path

        base = Path(os.environ.get("VEYA_WRITE_ROOT", str(Path.home() / ".veya" / "work")))
        bot_root = base / "supervised" / str(bot.bot_id)
        bot_root.mkdir(parents=True, exist_ok=True)
        project_root = str(bot_root)

    # Record the user's message so the turn is durable like any other
    # bot turn — the supervisor reply is the bot's answer to it.
    try:
        store = conversations if conversations is not None else conversation_store()
        store.append(
            bot.bot_id,
            role="user",
            text=str(text or ""),
        )
    except Exception:
        pass

    try:
        from server.goal_run.bot_identity import bind_current_bot, reset_current_bot
        from server.supervision_tools import supervise_task as _run

        # The phrase path runs before ``_run_product_task`` binds the bot scope,
        # so ``supervise_task`` would otherwise read ``current_bot_id() == None``
        # and file the binding/routine/report under "veya-default" instead of
        # this bot. Bind the turn's bot here so the supervised run is owned by it.
        bot_token = bind_current_bot(bot.bot_id)
        try:
            result = _run(
                project_root=project_root,
                objective=str(text or ""),
                report_every_minutes=int(parsed["report_every_minutes"]),
                stall_after_minutes=int(parsed.get("stall_after_minutes", 15)),
                bot_id=bot.bot_id,
                user=dict(user or {}),
                provider=getattr(bot, "provider", None) or None,
                model=getattr(bot, "model", None) or None,
                run_objective=build_objective(bot=bot, text=str(text or ""))
                if hasattr(bot, "name")
                else None,
            )
        finally:
            reset_current_bot(bot_token)
    except Exception as exc:
        return BotTurnResult(
            bot_id=bot.bot_id,
            task_id="",
            session_id="",
            reply=f"启动监工任务失败：{exc}",
            status="error",
            trace_id="",
        )

    binding_id = result.get("binding", {}).get("binding_id", "")
    reply = (
        f"已启动监工任务（binding: {binding_id}），每 {parsed['report_every_minutes']} "
        f"分钟汇报一次进度。卡住时最多自动续跑 {result.get('max_repairs', 3)} 次。"
    )
    return BotTurnResult(
        bot_id=bot.bot_id,
        task_id=result.get("goal_run_id", ""),
        session_id="",
        reply=reply,
        status="completed",
        trace_id="",
    )


def _coalesce_window_s() -> float:
    import os

    try:
        return max(0.0, float(os.environ.get("VEYA_TURN_COALESCE_S", "2.0") or 0))
    except ValueError:
        return 2.0


#: Friendly reply while a side-effect action waits on the user's approval.
APPROVAL_WAIT_REPLY = (
    "这个操作需要你批准后才会执行。我已经在批准列表里提交了申请，批准后我会继续，结果会发在这里。"
)

#: Turns still running after their caller got the "waiting for approval" reply.
_BACKGROUND_TURNS: set[Any] = set()


def _approval_gates_on() -> bool:
    try:
        from server.approvals import approval_gates_enabled

        return bool(approval_gates_enabled())
    except Exception:
        return False


def _new_pending_approvals(
    user: dict[str, Any], since: float, session_id: str = ""
) -> list[dict[str, Any]]:
    """Pending approvals *this turn* opened since ``since`` (epoch secs).

    Matched on the turn's session id, so another turn of the same user that is
    still waiting on its own approval is never mistaken for this one.
    """

    from server.approvals import get_approval_service

    service = get_approval_service()
    scope = service.scope_for(str((user or {}).get("user_id") or "anonymous"))
    rows = service.list_pending(scope=scope)
    return [
        row
        for row in rows
        if float(row.get("created_at") or 0) >= since
        and str(row.get("session_id") or "") == str(session_id or "")
    ]


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

    from server import user_control

    since = time.time() - 1.0
    # The turn's approvals carry its session id, which is how we recognise them.
    token = user_control._session_id.set(start.session_id)
    try:
        task = asyncio.ensure_future(finishing)
    finally:
        user_control._session_id.reset(token)
    while not task.done():
        done, _ = await asyncio.wait({task}, timeout=0.5)
        if done:
            break
        try:
            pending = _new_pending_approvals(user, since, start.session_id)
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
    "parse_supervisor_phrase",
    "run_bot_turn",
]
