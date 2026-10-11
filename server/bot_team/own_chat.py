"""read_own_chat — a bot searching its own conversation history (NANO-W10 ADDENDUM N).

Older parts and condensed messages stay in the **EXISTING** W2 session FTS index
(``veya.oservi.history_store``). This tool adds no index, no store and no cache:
it queries that index and renders each hit with a citable ``part N #id`` anchor.

Identity: the running bot (the GoalRun canonical worker binds it around every
physical action), and that bot's **owner** — the history store's FTS rows are
partitioned by ``user_id``, so a bot can only ever reach transcripts its owner
wrote. There is no way to pass a user id in from the model.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from runtime import bot_scope
from veya.oskill.pure.chat_anchors import build_anchor, render_snippet

__all__ = ["own_chat_enabled", "read_own_chat", "search_own_chat"]

#: Cap so one tool call cannot dump a whole transcript into the context.
MAX_HITS = 20


def own_chat_enabled() -> bool:
    """``VEYA_READ_OWN_CHAT`` (default OFF)."""
    from server.feature_flags import enabled as _flag_enabled

    try:
        return bool(_flag_enabled("VEYA_READ_OWN_CHAT"))
    except KeyError:
        return False


def _running_bot() -> str:
    bot_id = bot_scope.current_bot()
    if not bot_id:
        raise RuntimeError(
            "read_own_chat requires a bound bot scope (no running bot in this execution context)"
        )
    return bot_id


def _owner_of(bot_id: str) -> str:
    """Owner of *bot_id* from the existing bot registry — never from the model."""
    from server.goal_run.bot_identity import BotRegistry

    spec = BotRegistry().get(bot_id)
    return str(getattr(spec, "owner_id", "") or "")


async def search_own_chat(
    query: str,
    *,
    limit: int = 5,
    part: int = 0,
    bot_id: str | None = None,
) -> dict[str, Any]:
    """Search the calling bot owner's transcripts and return anchored hits."""
    running = bot_id or _running_bot()
    owner = _owner_of(running)
    if not owner:
        return {
            "ok": False,
            "bot_id": running,
            "hits": [],
            "error": "bot has no owner; refusing to search transcripts",
        }
    from veya.history_store import default_history_store

    store = default_history_store()
    capped = max(1, min(int(limit), MAX_HITS))
    results = await store.search(query=query, user_id=owner, limit=capped)
    hits: list[dict[str, Any]] = []
    for index, row in enumerate(results or [], start=1):
        anchor = build_anchor(part=int(part or 1), message_id=str(row.get("sid") or index))
        hits.append(render_snippet(str(row.get("snippet") or ""), anchor=anchor))
        hits[-1]["title"] = str(row.get("title") or "")
        hits[-1]["ts"] = int(row.get("ts") or 0)
    return {
        "ok": True,
        "bot_id": running,
        "query": query,
        "hits": hits,
        "search_backend": "session-fts",
    }


def read_own_chat(query: str, limit: int = 5, part: int = 0) -> str:
    """Search this bot's own earlier conversation and return anchored snippets.

    Use it when the user refers to something decided earlier ("what did we
    decide about X?") and the current context no longer holds it. Each hit
    carries a ``part N #id`` anchor you can quote back to the user.

    Flag: ``VEYA_READ_OWN_CHAT``. With the flag off this tool is not registered
    at all; calling the function directly returns a disabled envelope.
    """
    if not own_chat_enabled():
        return json.dumps(
            {"ok": False, "hits": [], "error": "read_own_chat is disabled"}, ensure_ascii=False
        )
    try:
        running = _running_bot()
    except RuntimeError as exc:
        return json.dumps({"ok": False, "hits": [], "error": str(exc)}, ensure_ascii=False)
    try:
        # The model-tool path runs inside a live event loop, so the blocking FTS
        # query goes to a daemon thread (the repo's established helper) instead of
        # asyncio.run, which would raise inside a running loop.
        from veya.obase.async_utils import run_sync_in_daemon_thread

        payload = asyncio.run(
            run_sync_in_daemon_thread(
                search_own_chat, query, limit=limit, part=part, bot_id=running
            )
        )
        return json.dumps(payload, ensure_ascii=False)
    except Exception as exc:
        return json.dumps(
            {
                "ok": False,
                "bot_id": running,
                "hits": [],
                "error": f"read_own_chat failed: {type(exc).__name__}: {exc}",
                "next_step": "retry with a shorter query, or ask the user to restate",
            },
            ensure_ascii=False,
        )


_READ_OWN_CHAT_DESCRIPTION = (
    "Search this bot's own earlier conversation (older parts and condensed "
    "messages stay in the session index). Use it when the user refers to "
    "something decided earlier that is no longer in the current context. "
    "Each hit comes back with a citable anchor like 'part 2 #1234'."
)

_READ_OWN_CHAT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "description": "What to look for in the earlier conversation.",
        },
        "limit": {
            "type": "integer",
            "description": "Max hits (default 5, cap 20).",
        },
        "part": {
            "type": "integer",
            "description": "Part number to anchor the hits with (default: current part).",
        },
    },
    "required": ["query"],
}


def register_tools(registry: Any) -> int:
    """Register ``read_own_chat`` into the existing registry (idempotent)."""
    if not own_chat_enabled():
        return 0
    if registry.has("read_own_chat"):
        return 0
    from server.tool_registry import SideEffect

    registry.register(
        "read_own_chat",
        _READ_OWN_CHAT_DESCRIPTION,
        _READ_OWN_CHAT_SCHEMA,
        read_own_chat,
        side_effect=SideEffect.PURE_READ,
        effect_capability="none",
        parallel_safe=True,
    )
    return 1