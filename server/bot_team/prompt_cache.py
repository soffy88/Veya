# 3O-IO-ALLOW — server/bot_team/prompt_cache.py
# 理由：本文件是 prompt-cache 纪律的**服务层接线**，不是新存储。usage 记录直接
# 复用既有 ``server/events.py`` 的 event_store（topic ``usage.recorded``，
# 白名单尾部追加），聚合与判定全部在 ``veya.oskill.pure.prompt_cache`` /
# ``veya.oprim.prompt_cache`` / ``veya.omodul.prompt_cache_metrics``。flag 关闭时
# 每个函数都是严格 no-op，不产生事件、不改上下文。

"""Server wiring for prompt-cache discipline and the context steward (W10-B).

Thin by construction: flags and identities live here, algorithms live in 3O.
Every entry point is a **strict no-op** when its flag is off, so turning the
feature on is the only way to change behaviour.
"""

from __future__ import annotations

import contextlib
import time
from typing import Any

from server.feature_flags import enabled as _flag_enabled
from veya.oskill.pure.prompt_cache import (
    assemble_messages,
    frozen_tool_set,
    prewarm_due,
    should_compact_idle,
)

__all__ = [
    "USAGE_TOPIC",
    "cache_hit_report",
    "prompt_cache_enabled",
    "read_cache_records",
    "record_call_usage",
    "record_typing_signal",
    "should_prewarm",
    "steward_enabled",
    "typing_says_compact_now",
    "turn_tool_freeze",
]

#: Appended to the existing event whitelist in ``server/events.py``. No new store.
USAGE_TOPIC = "usage.recorded"

#: Per-process typing/prewarm bookkeeping (debounce state, not durable state).
_TYPING_STATE: dict[str, dict[str, float]] = {}


def prompt_cache_enabled() -> bool:
    """``VEYA_PROMPT_CACHE`` (default OFF)."""
    try:
        return bool(_flag_enabled("VEYA_PROMPT_CACHE"))
    except KeyError:
        return False


def steward_enabled() -> bool:
    """``VEYA_CONTEXT_STEWARD`` (default OFF)."""
    try:
        return bool(_flag_enabled("VEYA_CONTEXT_STEWARD"))
    except KeyError:
        return False


def turn_tool_freeze(tools: list[Any]) -> dict[str, Any]:
    """Freeze the tool set for one turn.

    The tool list is hashed once at turn start; ``tool_search`` results ride as
    messages (existing W8 behaviour), never by mutating this list. Returns the
    digest the caller keeps for the turn.

    Flag off: returns an empty freeze without touching anything.
    """
    if not prompt_cache_enabled():
        return {"enabled": False, "names": [], "digest": ""}
    names, digest = frozen_tool_set(tools or [])
    return {"enabled": True, "names": list(names), "digest": digest}


def build_cached_messages(sections: list[tuple[str, str]]) -> list[dict[str, str]]:
    """Assemble provider messages in the one fixed section order.

    Flag off: the caller's own assembly is returned untouched (identity), so no
    frozen main-chain behaviour changes when the feature is off.
    """
    if not prompt_cache_enabled():
        return []
    return assemble_messages(sections)


def record_call_usage(
    *,
    session_id: str,
    usage: dict[str, Any],
    kind: str = "main",
    model: str = "",
    bot_id: str = "",
    sections: list[tuple[str, str]] | None = None,
    tools: list[Any] | None = None,
    frozen_tools_digest: str = "",
) -> dict[str, Any]:
    """Record one call's cached/uncached input tokens into the existing usage events.

    Flag off: returns a disabled envelope and writes nothing.

    Returns:
        ``{"enabled", "cached_input_tokens", "uncached_input_tokens", "ok"}``.
    """
    if not prompt_cache_enabled():
        return {
            "enabled": False,
            "ok": False,
            "cached_input_tokens": 0,
            "uncached_input_tokens": 0,
        }
    from veya.omodul.prompt_cache_metrics import (
        PromptCacheMetricsConfig,
        PromptCacheMetricsInput,
        prompt_cache_metrics,
    )
    from server.events import append_canonical_event

    def sink(record: dict[str, Any]) -> None:
        append_canonical_event(
            USAGE_TOPIC,
            {
                "kind": record.get("kind") or "main",
                "model": record.get("model") or "",
                "bot_id": record.get("bot_id") or "",
                "prompt_tokens": record.get("cached_input_tokens", 0)
                + record.get("uncached_input_tokens", 0),
                "cached_input_tokens": record.get("cached_input_tokens", 0),
                "uncached_input_tokens": record.get("uncached_input_tokens", 0),
                "output_tokens": record.get("output_tokens", 0),
                "cache_broken": record.get("cache_broken", False),
                "tools_digest": record.get("tools_digest", ""),
                "prefix_digests": record.get("prefix_digests", {}),
            },
            actor="system",
            session_id=session_id or None,
        )

    import asyncio

    coro = prompt_cache_metrics(
        PromptCacheMetricsConfig(usage_sink=sink),
        PromptCacheMetricsInput(
            session_ref=session_id,
            usage=usage or {},
            sections=sections or [],
            tools=tools or [],
            frozen_tools_digest=frozen_tools_digest or "",
            kind=kind,
            model=model,
            bot_id=bot_id,
        ),
    )
    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        running = None
    if running is not None:
        running.create_task(coro)  # fire-and-forget: never block a model call
        return {
            "enabled": True,
            "ok": True,
            "cached_input_tokens": 0,
            "uncached_input_tokens": 0,
        }
    with contextlib.suppress(Exception):
        asyncio.run(coro)
    return {"enabled": True, "ok": True, "cached_input_tokens": 0, "uncached_input_tokens": 0}


def read_cache_records(*, session_id: str | None = None) -> list[dict[str, Any]]:
    """Read existing usage events for the performance route.

    Flag off: always an empty list — the surface is not there to read.
    """
    if not prompt_cache_enabled():
        return []
    from server.events import event_store

    records = event_store.read_all(topics={USAGE_TOPIC})
    if session_id:
        records = [row for row in records if str(row.get("session_id") or "") == session_id]
    return list(records)


def cache_hit_report(*, session_id: str | None = None) -> dict[str, Any]:
    """Cache-hit rate over recorded calls. Flag off: zeros with ``enabled=False``."""
    from veya.oskill.pure.prompt_cache import usage_cache_rate

    if not prompt_cache_enabled():
        return {"enabled": False, **usage_cache_rate([])}
    rows = read_cache_records(session_id=session_id)
    payload = []
    for row in rows:
        payload.append(
            {
                "cached_input_tokens": int((row.get("payload") or {}).get("cached_input_tokens") or 0),
                "uncached_input_tokens": int(
                    (row.get("payload") or {}).get("uncached_input_tokens") or 0
                ),
            }
        )
    return {"enabled": True, **usage_cache_rate(payload)}


def record_typing_signal(session_id: str, *, now: float | None = None) -> dict[str, Any]:
    """Record a debounced typing signal; returns whether a prewarm is due.

    Flag off: always ``{"due": False, "enabled": False}`` and no state kept.
    """
    stamp = float(now if now is not None else time.time())
    if not prompt_cache_enabled():
        return {"due": False, "enabled": False}
    state = _TYPING_STATE.setdefault(session_id or "default", {})
    due = prewarm_due(
        last_signal_at=state.get("last_signal_at"),
        last_warm_at=state.get("last_warm_at"),
        now=stamp,
    )
    if due:
        state["last_warm_at"] = stamp
    state["last_signal_at"] = stamp
    return {"due": bool(due), "enabled": True, "session_id": session_id}


def should_prewarm(session_id: str, *, now: float | None = None) -> bool:
    """Whether the client should get a cache-warming request right now."""
    if not prompt_cache_enabled():
        return False
    state = _TYPING_STATE.get(session_id or "default", {})
    stamp = float(now if now is not None else time.time())
    return prewarm_due(
        last_signal_at=state.get("last_signal_at"),
        last_warm_at=state.get("last_warm_at"),
        now=stamp + 1e-6,
    )


def typing_says_compact_now(
    *,
    tokens: int,
    window: int,
    idle_seconds: float,
    warm_seconds: float = 300.0,
) -> bool:
    """Proactive compaction gate: big, idle, and the provider cache still warm.

    Flag off: always ``False`` — the existing compact path is untouched.
    """
    if not prompt_cache_enabled():
        return False
    return should_compact_idle(
        tokens=int(tokens),
        window=int(window),
        idle_seconds=float(idle_seconds),
        warm_seconds=float(warm_seconds),
    )