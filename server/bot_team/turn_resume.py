"""server.bot_team.turn_resume — durable suspend/resume for approval waits.

Autoloop graft, design item 1 (see ~/workspace/veya-autoloop-graft-design.md).
A turn that opens a pending approval answers "waiting for approval" and
keeps running in the background; if the process restarts or the 120s live
wait times out, the continuation used to be lost.  The suspension is now
recorded in the approvals database (``turn_resumes`` table, owned by
``ApprovalStore``); a resume is a NEW turn, never a revived coroutine.

Rules that keep this safe:

* Claim exactly once: only a ``pending`` row can become ``claimed``.  A
  second claimant gets nothing and does nothing.
* Resume only when every approval for the session resolved to an allow
  decision.  Any deny ends the resume with a visible note instead.
* The original objective is re-checked against the transcript at the
  recorded point: if the last user message before suspension changed, the
  resume does not run.
* A resume never fires while the original turn is still live in this
  process (the live turn owns the conversation).  After a restart the
  live set is empty, so the startup reaper can fire.
* Group turns resume into their room: the reply is appended to the room
  transcript the same way the group round loop does.  The room post
  budget is deliberately not applied to a resumed reply: the resume is
  an owner-approved continuation, not autonomous chatter.
* Every failure is contained and visible: a crashed resume writes a
  note into the conversation and closes the record instead of retrying
  forever.  Nothing here may break approval resolve or process startup.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

log = logging.getLogger("veya.turn_resume")

#: Resume records live this long; expiry only marks state, rows are kept.
RESUME_TTL_S = 7 * 24 * 3600

#: Decisions that let a suspended turn continue.
ALLOW_DECISIONS = frozenset({"allow", "once", "session", "always"})

#: Seams for tests.  ``None`` means "resolve lazily from the real modules".
_RUN_TURN: Callable[..., Awaitable[Any]] | None = None
_RESOLVE_BOT: Callable[[str], Any | None] | None = None


def objective_hash_for(text: str) -> str:
    """Stable hash of the objective text (the last user message)."""

    return hashlib.sha256(str(text or "").encode("utf-8")).hexdigest()


def suspension_snapshot(messages: list[Any]) -> tuple[int, str]:
    """(rev, objective hash) for the conversation as it stands now.

    ``rev`` is the message count; the objective is the last user message.
    """

    rev = len(messages)
    objective = ""
    for message in reversed(messages):
        item = message if isinstance(message, dict) else {}
        if item.get("role") == "user":
            objective = str(item.get("text") or "")
            break
    return rev, objective_hash_for(objective)


def record_suspension(
    *,
    store: Any | None = None,
    bot_id: str,
    session_id: str,
    task_id: str,
    trace_id: str = "",
    user_id: str = "",
    thread_id: str = "",
    request_ids: list[str] | None = None,
    rev: int = 0,
    objective_hash: str = "",
    now: float | None = None,
) -> dict[str, Any]:
    """Persist one suspension.  Raises on a store failure: the caller must
    tell the user the resume was NOT persisted (never pretend otherwise)."""

    if store is None:
        from veya.obase.approvals import ApprovalStore

        store = ApprovalStore()
    stamp = time.time() if now is None else float(now)
    return store.create_turn_resume(
        {
            "resume_id": f"res_{uuid.uuid4().hex[:16]}",
            "bot_id": bot_id,
            "session_id": session_id,
            "task_id": task_id,
            "trace_id": trace_id,
            "user_id": user_id,
            "thread_id": thread_id,
            "request_ids": list(request_ids or []),
            "rev": rev,
            "objective_hash": objective_hash,
            "created_at": stamp,
            "expires_at": stamp + RESUME_TTL_S,
        }
    )


def _outcomes_for(record: dict[str, Any], approvals: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_id = {str(a.get("request_id") or ""): a for a in approvals}
    outcomes = []
    for request_id in record.get("request_ids") or []:
        row = by_id.get(str(request_id))
        if row is not None:
            outcomes.append(
                {
                    "request_id": str(request_id),
                    "tool": str(row.get("tool") or ""),
                    "decision": row.get("decision"),
                    "status": row.get("status"),
                }
            )
    return outcomes


def evaluate_resume(
    record: dict[str, Any],
    *,
    approvals: list[dict[str, Any]],
    messages: list[Any],
) -> tuple[str, str, list[dict[str, Any]]]:
    """Decide a claimed record.  Returns (verdict, reason, outcomes).

    Verdicts: ``resume`` (run a new turn), ``done`` (close with a note),
    ``skip`` (still waiting; release the claim).
    """

    outcomes = _outcomes_for(record, approvals)
    by_id = {str(a.get("request_id") or ""): a for a in approvals}
    wanted = [str(rid) for rid in record.get("request_ids") or []]
    if any(rid not in by_id for rid in wanted):
        return "skip", "approval-missing", outcomes
    rows = [by_id[rid] for rid in wanted]
    if any(str(row.get("status") or "") != "resolved" for row in rows):
        return "skip", "waiting", outcomes
    if any(str(row.get("decision") or "") not in ALLOW_DECISIONS for row in rows):
        return "done", "denied", outcomes
    rev = int(record.get("rev") or 0)
    _, current_hash = suspension_snapshot(list(messages[:rev]))
    if current_hash != str(record.get("objective_hash") or ""):
        return "done", "objective-changed", outcomes
    return "resume", "ready", outcomes


def build_preamble(
    record: dict[str, Any],
    outcomes: list[dict[str, Any]],
    *,
    ended_before: bool,
) -> str:
    """The resume turn's input text: outcomes plus the continuation order."""

    lines = [
        "[续行] 上一轮（task_id="
        + str(record.get("task_id") or "")
        + "）在等批准时挂起了，批复已到，接着把原目标干完。",
        "批复：",
    ]
    for outcome in outcomes:
        lines.append(
            f"- {outcome.get('tool') or '操作'}（{outcome.get('request_id')}）：{outcome.get('decision')}"
        )
    if ended_before:
        lines.append("上一轮已结束（可能因等待超时），本次按新批复继续。")
    else:
        lines.append("上一轮因进程重启中断，本次按批复继续。")
    lines.append(
        "原目标见对话记录（挂起前最后一条用户消息）。只按原目标继续，不要另起新任务；"
        "如原目标已在对话里完成，先说明已完成，不要重复执行。"
    )
    return "\n".join(lines)


def _append_note(conversations: Any, record: dict[str, Any], text: str) -> None:
    """Best-effort visible note in the bot conversation.  Never raises."""

    try:
        conversations.append(
            str(record.get("bot_id") or ""),
            role="bot",
            text=text,
            task_id=str(record.get("task_id") or ""),
        )
    except Exception:
        log.exception("turn-resume note append failed for %s", record.get("resume_id"))


def _append_room_reply(record: dict[str, Any], turn: Any) -> None:
    """Mirror the group round loop: a resumed group reply lands in the room."""

    thread_id = str(record.get("thread_id") or "")
    if not thread_id:
        return
    try:
        from server.bot_team.groups import PASS_SENTINEL, GroupStore

        reply = str(getattr(turn, "reply", "") or "")
        GroupStore().append_message(
            thread_id,
            role="bot",
            bot_id=str(record.get("bot_id") or ""),
            text=reply if reply.strip() else PASS_SENTINEL,
            task_id=getattr(turn, "task_id", None),
        )
    except Exception:
        log.exception("turn-resume room append failed for %s", record.get("resume_id"))


def _task_live(task_id: str) -> bool:
    """True when the original turn is still running in this process."""

    try:
        from server.bot_team import turns as turns_mod

        live = getattr(turns_mod, "_AWAITING_TASKS", None)
        return bool(live) and str(task_id) in live
    except Exception:
        return False


def _resolve_bot(bot_id: str) -> Any | None:
    if _RESOLVE_BOT is not None:
        return _RESOLVE_BOT(bot_id)
    from server.goal_run.bot_identity import BotRegistry

    return BotRegistry().get(bot_id)


async def _run_turn(**kwargs: Any) -> Any:
    if _RUN_TURN is not None:
        return await _RUN_TURN(**kwargs)
    from server.bot_team.turns import run_bot_turn

    return await run_bot_turn(**kwargs)


async def run_resume_for_record(
    record: dict[str, Any],
    *,
    store: Any | None = None,
    conversations: Any | None = None,
    now: float | None = None,
) -> str:
    """Run one claimed resume to its terminal state.  Never raises."""

    if store is None:
        from veya.obase.approvals import ApprovalStore

        store = ApprovalStore()
    if conversations is None:
        from server.bot_team.conversations import ConversationStore

        conversations = ConversationStore()
    resume_id = str(record.get("resume_id") or "")
    try:
        claimed = store.claim_turn_resume(resume_id)
        if claimed is None:
            return "already-claimed"
        record = claimed
        approvals = store.list_approvals(
            session_id=str(record.get("session_id") or ""), limit=100
        )
        messages = conversations.list(str(record.get("bot_id") or ""))
        verdict, reason, outcomes = evaluate_resume(
            record, approvals=approvals, messages=messages
        )
        if verdict == "skip":
            store.release_turn_resume(resume_id)
            return f"waiting-{reason}"
        if verdict == "done":
            if reason == "denied":
                detail = "、".join(
                    f"{o.get('tool') or '操作'}：{o.get('decision')}" for o in outcomes
                )
                _append_note(conversations, record, f"已否决，未继续：{detail}。")
            elif reason == "objective-changed":
                _append_note(conversations, record, "续行校验未过：原目标已变，未继续。")
            store.transition_turn_resume(resume_id, "done")
            return f"done-{reason}"
        rev = int(record.get("rev") or 0)
        ended_before = any(
            (m if isinstance(m, dict) else {}).get("task_id") == record.get("task_id")
            and (m if isinstance(m, dict) else {}).get("role") == "bot"
            for m in messages[rev:]
        )
        bot = _resolve_bot(str(record.get("bot_id") or ""))
        if bot is None:
            _append_note(conversations, record, "续行未跑：bot 已不存在。")
            store.transition_turn_resume(resume_id, "done")
            return "done-bot-missing"
        preamble = build_preamble(record, outcomes, ended_before=ended_before)
        turn = await _run_turn(
            bot=bot,
            text=preamble,
            user={"user_id": str(record.get("user_id") or "")},
            conversations=conversations,
            thread_id=str(record.get("thread_id") or ""),
        )
        _append_room_reply(record, turn)
        store.transition_turn_resume(resume_id, "done")
        return "resumed"
    except Exception as exc:
        log.exception("turn-resume failed for %s", resume_id)
        with contextlib.suppress(Exception):
            _append_note(conversations, record, f"续行失败：{exc}，请重说一次。")
        with contextlib.suppress(Exception):
            store.transition_turn_resume(resume_id, "done")
        return "failed"


def maybe_mark_done(
    resume_id: str,
    *,
    store: Any | None = None,
    request_ids: list[str] | None = None,
    session_id: str = "",
) -> bool:
    """Best-effort close: the original turn finished and its approvals all
    resolved, so no resume is owed.  Never raises; returns False on any
    doubt (the record stays pending for the reaper/hook to judge)."""

    try:
        if store is None:
            from veya.obase.approvals import ApprovalStore

            store = ApprovalStore()
        approvals = store.list_approvals(session_id=session_id, limit=100)
        by_id = {str(a.get("request_id") or ""): a for a in approvals}
        wanted = list(request_ids or [])
        if not wanted:
            return False
        rows = [by_id.get(str(rid)) for rid in wanted]
        if any(row is None or str(row.get("status") or "") != "resolved" for row in rows):
            return False
        return store.transition_turn_resume(str(resume_id), "done") is not None
    except Exception:
        log.exception("turn-resume mark-done failed for %s", resume_id)
        return False


async def maybe_resume_for_session(
    session_id: str,
    *,
    store: Any | None = None,
    conversations: Any | None = None,
) -> list[str]:
    """Resolve-hook entry: resume pending records for one session whose
    original turns are no longer live.  Never raises."""

    outcomes: list[str] = []
    if not session_id:
        return outcomes
    try:
        if store is None:
            from veya.obase.approvals import ApprovalStore

            store = ApprovalStore()
        pending = store.list_turn_resumes(state="pending", session_id=session_id)
        for record in pending:
            if _task_live(str(record.get("task_id") or "")):
                outcomes.append("skipped-live")
                continue
            try:
                outcomes.append(
                    await run_resume_for_record(
                        record, store=store, conversations=conversations
                    )
                )
            except Exception:
                log.exception(
                    "turn-resume session sweep failed for %s", record.get("resume_id")
                )
                outcomes.append("failed")
    except Exception:
        log.exception("turn-resume session sweep failed for %s", session_id)
    return outcomes


def schedule_resume_check(session_id: str) -> None:
    """Fire-and-forget resume sweep after an approval resolves.  Never
    raises, never blocks the resolve response."""

    if not session_id:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        log.warning("turn-resume check skipped: no running loop for %s", session_id)
        return
    task = loop.create_task(
        maybe_resume_for_session(session_id), name="veya-turn-resume-check"
    )

    def _done(done_task: asyncio.Task) -> None:
        try:
            done_task.result()
        except Exception:
            log.exception("turn-resume check failed for %s", session_id)

    task.add_done_callback(_done)


async def reap_turn_resumes(
    *,
    store: Any | None = None,
    conversations: Any | None = None,
    now: float | None = None,
) -> dict[str, int]:
    """Startup reaper: expire the overdue, resume the ready.  Never raises."""

    counts: dict[str, int] = {
        "expired": 0,
        "resumed": 0,
        "done": 0,
        "waiting": 0,
        "skipped-live": 0,
        "failed": 0,
    }
    try:
        if store is None:
            from veya.obase.approvals import ApprovalStore

            store = ApprovalStore()
        stamp = time.time() if now is None else float(now)
        try:
            counts["expired"] = int(store.expire_overdue_turn_resumes(stamp))
        except Exception:
            log.exception("turn-resume expiry sweep failed")
        try:
            pending = store.list_turn_resumes(state="pending")
        except Exception:
            log.exception("turn-resume pending list failed")
            return counts
        for record in pending:
            if _task_live(str(record.get("task_id") or "")):
                counts["skipped-live"] += 1
                continue
            try:
                outcome = await run_resume_for_record(
                    record, store=store, conversations=conversations
                )
            except Exception:
                log.exception(
                    "turn-resume reap failed for %s", record.get("resume_id")
                )
                outcome = "failed"
            if outcome == "resumed":
                counts["resumed"] += 1
            elif outcome.startswith("done-"):
                counts["done"] += 1
            elif outcome.startswith("waiting"):
                counts["waiting"] += 1
            elif outcome == "failed":
                counts["failed"] += 1
    except Exception:
        log.exception("turn-resume reap failed")
    return counts
