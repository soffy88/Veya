# 3O-IO-ALLOW — server/bot_team/turn_end.py
# 理由：回合末钩子的**接线**。记忆落盘全部委托 ``LayeredMemoryStore``；判定全部在
# ``veya.oskill.pure.*``；steward / 复审事务在 ``veya.omodul.*``。本文件本身没有
# 新的存储与调度，只在既有 turn 收尾处挂三个可选钩子。flag 关时全部 no-op。

"""Turn-end hooks for NANO-W10 group B (steward, daily log, memory review).

Called from :func:`server.bot_team.turns.finish_bot_turn` after the reply is
recorded. Three independent, independently-flagged hooks:

``VEYA_PROMPT_CACHE``   proactive compaction gate (pure judgement, no writes)
``VEYA_CONTEXT_STEWARD`` CHECKPOINT / HANDOFF decision at the check line
``VEYA_BOT_MEMORY_MD``  append the "what was done" line to today's daily log

Each hook is wrapped so a failure can never fail the turn: the user already has
their reply, and a memory write is not worth losing it over.
"""

from __future__ import annotations

import contextlib
import logging
import time
from typing import Any

__all__ = ["maybe_steward_turn_end", "record_turn_end_memory", "turn_end_hooks"]

logger = logging.getLogger("veya.bot_team.turn_end")


async def record_turn_end_memory(
    *,
    bot_id: str,
    reply: str,
    status: str = "",
    session_id: str = "",
) -> dict[str, Any]:
    """Append the daily-log line and (flag-gated) run the memory review.

    Returns ``{"ok", "daily", "review"}``; never raises.
    """
    out: dict[str, Any] = {"ok": True, "daily": {}, "review": {}}
    if not bot_id:
        return out
    from server.bot_team.memory import bot_memory_enabled

    if not bot_memory_enabled():
        return out
    from server.bot_team.memory_layers import LayeredMemoryStore

    store = LayeredMemoryStore(bot_id)
    what = " ".join(str(reply or "").split())[:200] or f"turn {status or 'finished'}"
    with contextlib.suppress(Exception):
        out["daily"] = store.append_daily(what, caller_bot_id=bot_id)
    with contextlib.suppress(Exception):
        from server.bot_team.memory_review_bridge import run_memory_review

        out["review"] = await run_memory_review(
            bot_id=bot_id,
            messages=[{"role": "assistant", "content": str(reply or "")[:2000]}],
            session_id=session_id,
        )
    return out


async def maybe_steward_turn_end(
    *,
    session_id: str,
    messages: list[dict[str, Any]],
    part: int = 1,
    window: int = 128_000,
    reply_reserve: int = 4_000,
) -> dict[str, Any]:
    """Run the context steward for one turn end.

    Returns the omodul envelope; ``{"action": "none"}`` when the flag is off,
    the conversation is below the check line, or the model gave nothing usable.
    The **caller** decides what to do with a CHECKPOINT/HANDOFF — this function
    never rewrites a conversation on its own.
    """
    from server.bot_team.prompt_cache import steward_enabled

    if not steward_enabled():
        return {"status": "skipped", "action": "none", "error": None}
    from server.bot_team.memory_review_bridge import _llm_caller
    from veya.omodul.context_steward import (
        ContextStewardConfig,
        ContextStewardInput,
        context_steward,
    )

    with contextlib.suppress(Exception):
        return await context_steward(
            ContextStewardConfig(
                llm_caller=_llm_caller, window=window, reply_reserve=reply_reserve
            ),
            ContextStewardInput(session_ref=session_id, part=part, messages=list(messages)),
        )
    return {"status": "skipped", "action": "none", "error": None}


async def turn_end_hooks(
    *,
    bot_id: str,
    reply: str,
    status: str = "",
    session_id: str = "",
    messages: list[dict[str, Any]] | None = None,
    part: int = 1,
    idle_seconds: float = 0.0,
) -> dict[str, Any]:
    """Run every turn-end hook; never raises, never blocks the reply."""
    from server.bot_team.prompt_cache import prompt_cache_enabled, typing_says_compact_now

    out: dict[str, Any] = {
        "compaction_due": False,
        "steward": {"action": "none"},
        "memory": {},
    }
    if prompt_cache_enabled() and messages:
        with contextlib.suppress(Exception):
            from veya.oskill.pure.context_compress import estimate_messages_tokens

            tokens = int(estimate_messages_tokens(messages))
            out["compaction_due"] = typing_says_compact_now(
                tokens=tokens, window=128_000, idle_seconds=idle_seconds or 1.0
            )
    with contextlib.suppress(Exception):
        out["steward"] = await maybe_steward_turn_end(
            session_id=session_id, messages=messages or [], part=part
        )
    with contextlib.suppress(Exception):
        out["memory"] = await record_turn_end_memory(
            bot_id=bot_id, reply=reply, status=status, session_id=session_id
        )
    return out


def stamp_part() -> int:
    """Current wall-clock marker used when a caller has no part counter."""
    return int(time.time())
