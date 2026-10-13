# 3O-IO-ALLOW — server/bot_team/memory_review_bridge.py
# 理由：复审事务的**接线**。所有文件读/写都委托给 ``LayeredMemoryStore``
# （server/bot_team/memory_layers.py），LLM 调用委托给
# ``veya.omodul.memory_review``，规则判定在 ``veya.oskill.pure.memory_layers``。
# 本文件不新增任何存储：journal 写的是既有的 ``memory/journal.jsonl``。
#
# ``VEYA_MEMORY_REVIEW``（默认 OFF）。flag 关时 ``run_memory_review`` 直接返回
# disabled envelope，一次模型调用都不发生。

"""Server wiring for the memory-review pass (NANO-W10 ADDENDUM O).

Bridges the turn end to ``veya.omodul.memory_review``: injects the model, the
existing layered store, the journal, and the scope decision. The model proposes;
the local rules (``entry_allowed``) and the caps decide. Nothing here can erase a
user-written line on its own — ``forget``/``remove_entry`` are explicit calls, and
a review that says ``remove`` only records the intent.
"""

from __future__ import annotations

import time
from typing import Any

from server.bot_team.memory_layers import TEAM_SCOPE_ID, LayeredMemoryStore
from veya.omodul.memory_review import (
    MemoryReviewConfig,
    MemoryReviewInput,
    memory_review,
)

__all__ = [
    "memory_review_enabled",
    "run_memory_review",
    "team_writers",
]


def memory_review_enabled() -> bool:
    """``VEYA_MEMORY_REVIEW`` (default OFF)."""
    from server.feature_flags import enabled as _flag_enabled

    try:
        return bool(_flag_enabled("VEYA_MEMORY_REVIEW"))
    except KeyError:
        return False


def team_writers(bot_id: str) -> bool:
    """Whether *bot_id* may write TEAM memory (injected decision, default deny).

    Layer 4 owns who is a Chef / group admin; this module only consumes the
    answer. Default is "no", so an unreviewed deployment can never grow a shared
    memory that a member bot silently wrote into.
    """
    from server.bot_team.chef import CHEF_BOT_ID

    return str(bot_id) == str(CHEF_BOT_ID)


async def _llm_caller(messages: list[dict[str, Any]]) -> dict[str, Any]:
    """Call the existing model path (same provider resolution as any tool call)."""
    import veya.obase.llm as llm

    response = await llm.llm_call(messages, max_tokens=600)
    choices = response.get("choices") or [{}]
    message = choices[0].get("message") or {} if choices else {}
    usage = response.get("usage") or {}
    from veya.obase._llm_config import calc_cost

    return {
        "content": str(message.get("content") or ""),
        "usage": usage,
        "cost_usd": float(calc_cost("veya1.2", usage) or 0.0),
    }


async def run_memory_review(
    *,
    bot_id: str,
    messages: list[dict[str, Any]],
    session_id: str = "",
    anchor: str = "",
    subject_ref: str = "",
) -> dict[str, Any]:
    """Run one memory-review pass for *bot_id*.

    Args:
        bot_id: the bot whose memory is reviewed.
        messages: this turn's new messages (the model's own proposal input).
        session_id: session reference used for the fingerprint.
        anchor: entry anchor (``part N #id``).
        subject_ref: pseudonymised caller reference for the trail.

    Returns:
        The omodul envelope. Flag off: ``{"status": "skipped", "action": "none"}``.
    """
    if not memory_review_enabled():
        return {"status": "skipped", "error": None, "action": "none", "reason": "flag off"}
    store = LayeredMemoryStore(bot_id)
    return await memory_review(
        MemoryReviewConfig(
            llm_caller=_llm_caller,
            read_entries=lambda _bot, scope: store.core_text(scope),
            write_entry=_make_writer(store),
            journal=lambda record: store._journal(record),
            scope_of=lambda _bot: TEAM_SCOPE_ID if team_writers(_bot) else "bot",
            allow_team_scope=True,
        ),
        MemoryReviewInput(
            session_ref=session_id or bot_id,
            bot_id=bot_id,
            messages=list(messages or []),
            anchor=anchor or f"part 1 #{str(session_id or bot_id)[:8]}",
            added=time.strftime("%Y-%m-%d", time.gmtime()),
            subject_ref=subject_ref,
        ),
    )


def _make_writer(store: LayeredMemoryStore):
    """Build the injected entry writer (append or forget) over the layered store."""

    def write(action: str, entry: str, *, scope: str = "bot", bot_id: str = "") -> dict[str, Any]:
        target_scope = scope if scope == TEAM_SCOPE_ID else "bot"
        from server.bot_team.memory_v2_mirror import mirror_forget, mirror_remember

        if action == "remove":
            result = store.remove_entry(entry, scope=target_scope)
            if result.get("ok") and target_scope == "bot":
                mirror_forget(store.bot_id, [entry])
            return result if result.get("ok") else {}
        try:
            written = store.append_entry(entry, scope=target_scope)
        except (ValueError, PermissionError):
            # Past the cap: the save fails with "make room", never truncates.
            return {}
        if written.get("ok") and target_scope == "bot":
            # Personal MemoryRecord v2 copy of the reviewed entry (bot scope only;
            # TEAM memory is shared, not one user's).
            mirror_remember(store.bot_id, entry, entrypoint="bot_memory_review", confidence=0.7)
        return written

    return write
