"""Mirror per-bot core-memory writes into the Personal Runtime (MemoryRecord v2).

Why this exists: a bot's "请记住…" goes through the ``remember`` tool (and the
turn-end memory review) into the bot's ``MEMORY.md`` file only.  The canonical
Personal Runtime -- ``memory_records`` in the execution database -- never saw
those facts, so Memory V2 stayed empty in production while bots were happily
remembering things.

Contract:
- The file store stays the bot's working memory; nothing here changes what the
  bot reads or writes there.  This module only *adds* a durable V2 copy.
- An explicit ``remember`` is a user instruction, so it is committed (candidate
  -> commit, the normal V2 provenance path).  A conflict with an existing fact
  is left as a reviewable candidate, never forced.  An entry the turn-end
  memory *review* (a model proposal) wrote is mirrored as a candidate only.
- ``forget`` marks the owner's matching active V2 records forgotten, so a fact
  the user removed from the bot does not survive in V2.
- Records are user-scoped to the bot's owner, so the existing user-boundary
  checks on ``/api/v1/memory`` apply to them.
- A mirror failure never fails the bot's turn, but it is logged (not swallowed).
- Active when the execution database is configured; ``VEYA_BOT_MEMORY_V2_MIRROR=0``
  turns it off, ``=1`` forces it on.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from typing import Any

logger = logging.getLogger("veya.bot_team.memory_v2_mirror")

__all__ = [
    "mirror_enabled",
    "mirror_forget",
    "mirror_forget_async",
    "mirror_remember",
    "mirror_remember_async",
]

_BG_TASKS: set[asyncio.Task[Any]] = set()
# The file store's "[source: ...; added: ...; by: ...]" suffix is file metadata,
# not part of the fact.
_META = re.compile(r"\s*\[source:[^\]]*\]\s*$")


def mirror_enabled() -> bool:
    """On when the execution database (the V2 authority) is configured, unless
    ``VEYA_BOT_MEMORY_V2_MIRROR=0``; ``=1`` forces it on (tests, SQLite dev)."""
    raw = os.environ.get("VEYA_BOT_MEMORY_V2_MIRROR", "").strip().lower()
    if raw in {"0", "false", "off", "no"}:
        return False
    v2 = os.environ.get("VEYA_MEMORY_V2", "1").strip().lower()
    if v2 in {"", "0", "false", "off", "no"}:
        return False
    if raw in {"1", "true", "on", "yes"}:
        return True
    return bool(os.environ.get("VEYA_EXECUTION_DATABASE_URL"))


def _owner_of(bot_id: str) -> str:
    """The bot's owner user id; falls back to the request's user."""
    try:
        from server.goal_run.bot_identity import BotRegistry

        spec = BotRegistry().get(bot_id)
        if spec is not None and spec.owner_id:
            return str(spec.owner_id)
    except Exception:  # registry unreadable: fall through to the request user
        logger.debug("bot registry lookup failed for %s", bot_id, exc_info=True)
    try:
        from server import auth as auth_mod

        user_id = str((auth_mod.current_user() or {}).get("user_id") or "")
        if user_id and user_id != "anonymous":
            return user_id
    except Exception:
        logger.debug("no request user for bot %s", bot_id, exc_info=True)
    return ""


async def mirror_remember_async(
    bot_id: str,
    text: str,
    *,
    entrypoint: str = "bot_remember",
    confidence: float = 0.9,
    memory_type: str = "semantic",
    owner_id: str | None = None,
    commit: bool = True,
) -> dict[str, Any]:
    """Write one bot-memory fact into V2: committed for an explicit user
    instruction, a reviewable candidate for a model-proposed entry."""
    if not mirror_enabled():
        return {"status": "skipped", "reason": "mirror off"}
    content = _META.sub("", str(text or "")).strip()
    if not bot_id or not content:
        return {"status": "skipped", "reason": "empty"}
    owner = owner_id or _owner_of(bot_id)
    if not owner:
        return {"status": "skipped", "reason": "bot has no owner"}
    from runtime.personal import PersonalRuntimeError, get_personal_runtime

    store = get_personal_runtime()
    event = await store.record_event(
        "memory.explicit_write",
        {"bot_id": bot_id, "content": content, "entrypoint": entrypoint},
    )
    candidate = await store.create_memory_candidate(
        content,
        scope_type="user",
        scope_id=owner,
        memory_type=memory_type,
        source_event_ids=[str(event["id"])],
        confidence=confidence,
        reason=f"bot memory ({entrypoint}) bot={bot_id}",
        provenance={"actor": owner, "bot_id": bot_id, "entrypoint": entrypoint},
    )
    if not commit:
        return {"status": "candidate", "candidate_id": candidate["id"]}
    try:
        committed = await store.commit_memory_candidate(candidate["id"])
    except PersonalRuntimeError as exc:
        # Conflicts stay reviewable candidates; that is the V2 contract.
        logger.warning(
            "bot memory V2 mirror left candidate %s uncommitted: %s %s",
            candidate["id"],
            getattr(exc, "code", ""),
            exc,
        )
        return {"status": "candidate", "candidate_id": candidate["id"], "code": exc.code}
    record = (committed or {}).get("record") or {}
    return {"status": "committed", "candidate_id": candidate["id"], "record_id": record.get("id")}


async def mirror_forget_async(
    bot_id: str, needles: list[str], *, owner_id: str | None = None
) -> dict[str, Any]:
    """Forget the owner's active V2 records that contain any of *needles*."""
    if not mirror_enabled():
        return {"status": "skipped", "reason": "mirror off"}
    needles = [n for n in (str(x or "").strip() for x in needles) if n]
    owner = owner_id or _owner_of(bot_id)
    if not bot_id or not needles or not owner:
        return {"status": "skipped", "reason": "nothing to forget"}
    from runtime.personal import get_personal_runtime

    store = get_personal_runtime()
    forgotten: list[str] = []
    for needle in needles:
        for record in await store.search_memory(
            needle, scope_type="user", scope_id=owner, limit=50
        ):
            rid = str(record.get("id") or "")
            if rid and rid not in forgotten and needle in str(record.get("content") or ""):
                await store.forget_memory(rid)
                forgotten.append(rid)
    return {"status": "ok", "forgotten": forgotten}


def _dispatch(coro: Any, what: str) -> dict[str, Any]:
    """Run *coro* from sync code: on the runtime's loop, or schedule it if we are
    already on a running loop (cannot block it)."""
    from runtime.personal import get_personal_runtime

    store = get_personal_runtime()
    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        running = None
    try:
        if running is not None:
            task = running.create_task(coro)
            _BG_TASKS.add(task)

            def _done(t: asyncio.Task[Any]) -> None:
                _BG_TASKS.discard(t)
                if not t.cancelled() and t.exception() is not None:
                    logger.warning("bot memory V2 %s failed: %r", what, t.exception())

            task.add_done_callback(_done)
            return {"status": "scheduled"}
        owner_loop = getattr(store, "_loop", None)
        if owner_loop is not None and owner_loop.is_running():
            return asyncio.run_coroutine_threadsafe(coro, owner_loop).result(timeout=20)
        return asyncio.run(coro)
    except Exception as exc:  # never fail the bot turn over the mirror
        logger.warning("bot memory V2 %s failed: %r", what, exc)
        return {"status": "error", "error": str(exc)[:200]}


def mirror_remember(bot_id: str, text: str, **kwargs: Any) -> dict[str, Any]:
    if not mirror_enabled():
        return {"status": "skipped", "reason": "mirror off"}
    kwargs.setdefault("owner_id", _owner_of(bot_id))
    return _dispatch(mirror_remember_async(bot_id, text, **kwargs), "remember")


def mirror_forget(bot_id: str, needles: list[str], **kwargs: Any) -> dict[str, Any]:
    if not mirror_enabled():
        return {"status": "skipped", "reason": "mirror off"}
    kwargs.setdefault("owner_id", _owner_of(bot_id))
    return _dispatch(mirror_forget_async(bot_id, needles, **kwargs), "forget")
