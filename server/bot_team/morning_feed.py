"""server/bot_team/morning_feed.py — 晨间简报 delivery (Layer 4).

Service-layer driver for the NANO-W7 morning feed. Responsibilities that
belong to the service layer, not to the omodul:

* gather the feed sections from the existing stores
* assemble the ``daily_brief`` omodul into the ``sequential_composer``
  engine via ``server.assembly.assemble_morning_feed``
* deliver the result through the existing Web Push (P5) and the channel
  adapters (NANO-W7 §1)

No second scheduler: the trigger is the existing routine clock, which calls
``run_morning_feed`` from the routine dispatch path.
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_log = logging.getLogger(__name__)

CONFIG_FILE = "morning_feed.json"


def default_feed_root() -> Path:
    """Root for morning-feed artifacts and per-user config."""
    raw = os.environ.get("VEYA_MORNING_FEED_ROOT") or str(Path.home() / ".veya" / "morning_feed")
    return Path(raw).expanduser()


def artifacts_dir() -> Path:
    """Where brief artifacts are written."""
    return default_feed_root() / "artifacts"


def config_path() -> Path:
    """Where per-user feed configuration is persisted."""
    return default_feed_root() / CONFIG_FILE


@dataclass
class MorningFeedConfig:
    """Per-user feed configuration (keyed by the stable owner reference)."""

    owner_ref: str
    enabled: bool = True
    time: str = "08:00"
    timezone: str = "Asia/Shanghai"
    sections: list[str] = field(
        default_factory=lambda: ["routines", "goals", "goalruns", "memory", "rss"]
    )
    rss_urls: list[str] = field(default_factory=list)
    push_webpush: bool = True
    push_channels: list[str] = field(default_factory=list)
    max_items_per_section: int = 5

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> MorningFeedConfig:
        defaults = cls(owner_ref="")
        return cls(
            owner_ref=str(value.get("owner_ref", "")),
            enabled=bool(value.get("enabled", True)),
            time=str(value.get("time", "08:00"))[:5],
            timezone=str(value.get("timezone", "Asia/Shanghai")),
            sections=[str(s) for s in value.get("sections", defaults.sections)],
            rss_urls=[str(u) for u in value.get("rss_urls", [])],
            push_webpush=bool(value.get("push_webpush", True)),
            push_channels=[str(p) for p in value.get("push_channels", [])],
            max_items_per_section=int(value.get("max_items_per_section", 5)),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "owner_ref": self.owner_ref,
            "enabled": self.enabled,
            "time": self.time,
            "timezone": self.timezone,
            "sections": list(self.sections),
            "rss_urls": list(self.rss_urls),
            "push_webpush": self.push_webpush,
            "push_channels": list(self.push_channels),
            "max_items_per_section": self.max_items_per_section,
        }


def load_configs() -> dict[str, dict[str, Any]]:
    """All stored per-user feed configs (tolerant of a missing/corrupt file)."""
    try:
        raw = json.loads(config_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    feeds = raw.get("feeds")
    if not isinstance(feeds, dict):
        return {}
    return {str(k): v for k, v in feeds.items() if isinstance(v, dict)}


def get_config(owner_ref: str) -> MorningFeedConfig:
    """Config for one owner, falling back to defaults."""
    stored = load_configs().get(owner_ref)
    if not stored:
        return MorningFeedConfig(owner_ref=owner_ref)
    return MorningFeedConfig.from_dict({**stored, "owner_ref": owner_ref})


def save_config(config: MorningFeedConfig) -> dict[str, Any]:
    """Persist one owner's feed config."""
    from server.bot_team.conversations import atomic_write_json

    feeds = load_configs()
    feeds[config.owner_ref] = config.to_dict()
    atomic_write_json(config_path(), {"feeds": feeds})
    return config.to_dict()


def due_configs(*, now: float | None = None) -> list[MorningFeedConfig]:
    """Enabled configs whose local brief time has arrived today."""
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    moment = datetime.fromtimestamp(now if now is not None else time.time())
    due: list[MorningFeedConfig] = []
    for owner_ref in load_configs():
        config = get_config(owner_ref)
        if not config.enabled:
            continue
        try:
            tz = ZoneInfo(config.timezone)
        except Exception:
            _log.warning("Unknown timezone %s for %s", config.timezone, owner_ref)
            continue
        local = moment.astimezone(tz)
        try:
            hour, minute = (int(part) for part in config.time.split(":")[:2])
        except ValueError:
            continue
        target = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if target <= local < target + timedelta(minutes=15):
            due.append(config)
    return due


async def gather_sections(config: MorningFeedConfig) -> dict[str, Any]:
    """Collect feed sections from the existing stores.

    Missing sources degrade to an empty section rather than failing the brief.
    """
    return {
        "routines": await _gather_routines(config.owner_ref),
        "goals": [],
        "goalruns": [],
        "memory_highlights": await _gather_memory(config.owner_ref),
        "rss_items": [],
    }


async def _gather_routines(owner_ref: str) -> list[dict[str, Any]]:
    from server.goal_run.routine_registry import RoutineRegistry

    try:
        return [spec.to_dict() for spec in RoutineRegistry().list(include_disabled=True)]
    except Exception:
        _log.debug("routine list unavailable for %s", owner_ref, exc_info=True)
        return []


async def _gather_memory(owner_ref: str) -> list[dict[str, Any]]:
    """Most recent memories for this owner, as brief highlights."""
    try:
        from runtime.personal.runtime import get_personal_runtime

        rows = await get_personal_runtime().search_memory(limit=5)
    except Exception:
        _log.debug("memory highlights unavailable for %s", owner_ref, exc_info=True)
        return []
    highlights: list[dict[str, Any]] = []
    for row in rows or []:
        summary = row.get("summary") or row.get("content") or row.get("text") or ""
        if summary:
            highlights.append({"summary": str(summary)})
    return highlights


async def run_morning_feed(
    config: MorningFeedConfig,
    *,
    sections: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Generate and deliver one user's morning brief.

    Returns a delivery report; never raises.
    """
    from veya.omodul.daily_brief import (
        DailyBriefConfig,
        DailyBriefInput,
        daily_brief,
    )

    feed_config = DailyBriefConfig(
        owner_ref=config.owner_ref,
        timezone=config.timezone,
        brief_time=config.time,
        sections=list(config.sections),
        max_items_per_section=config.max_items_per_section,
        push_channels=list(config.push_channels),
    )
    input_data = DailyBriefInput(
        **(sections if sections is not None else await gather_sections(config))
    )

    owner_dir = artifacts_dir() / _safe_segment(config.owner_ref)
    result = await daily_brief(feed_config, input_data, owner_dir)

    if result.get("status") != "completed":
        _log.warning("daily_brief failed for %s: %s", config.owner_ref, result.get("error"))
        return result

    findings = result.get("findings") or {}
    markdown = str(findings.get("markdown", ""))
    delivery: dict[str, Any] = {"webpush": False, "channels": {}}

    if config.push_webpush:
        delivery["webpush"] = await _push_webpush(config.owner_ref, markdown)

    if config.push_channels:
        delivery["channels"] = await _deliver_channels(
            config.owner_ref,
            findings.get("channel_payloads") or {},
        )

    result["delivery"] = delivery
    return result


async def _push_webpush(owner_ref: str, markdown: str) -> bool:
    """Deliver via the existing Web Push notifier (P5)."""
    try:
        from server.bot_team.push import PushPayload, get_push_notifier

        preview = markdown[:200] + "…" if len(markdown) > 200 else markdown
        outcome = get_push_notifier().send(
            user_id=owner_ref,
            payload=PushPayload(title="☀️ 晨间简报", body=preview, kind="daily_brief"),
        )
        return bool(outcome.get("sent"))
    except Exception:
        _log.warning("Web Push delivery failed for %s", owner_ref, exc_info=True)
        return False


async def _deliver_channels(owner_ref: str, payloads: dict[str, Any]) -> dict[str, bool]:
    """Post the pre-shaped payloads to each owner's bound chats."""
    from server.channels import adapter_for, binding_store
    from server.channels.bindings import ChannelBindingStore

    delivered: dict[str, bool] = {}
    store: ChannelBindingStore = binding_store()
    for platform, messages in payloads.items():
        ok = False
        try:
            client = await _client(platform)
            adapter = adapter_for(platform)
            for binding in store.list(owner_ref=owner_ref):
                if binding.get("platform") != platform:
                    continue
                await adapter.send_reply(
                    client,
                    external_chat_id=str(binding.get("external_chat_id", "")),
                    text="\n\n".join(str(m) for m in messages),
                )
                ok = True
        except Exception:
            _log.warning("Channel delivery failed on %s", platform, exc_info=True)
        delivered[platform] = ok
    return delivered


async def _client(platform: str) -> Any:
    from server.channels.clients import client_for

    return await client_for(platform)


def _safe_segment(value: str) -> str:
    """Make ``value`` safe as a single path segment."""
    import re

    cleaned = re.sub(r"[^A-Za-z0-9._-]", "_", value)[:64]
    return cleaned or "anonymous"


# Routine ids the morning feed owns. Everything else keeps the default path.
FEED_ROUTINE_PREFIX = "daily_brief"


def is_feed_routine(routine_id: str) -> bool:
    """Whether ``routine_id`` is a morning-feed routine."""
    return str(routine_id).startswith(FEED_ROUTINE_PREFIX)


async def _dispatch_feed(handoffs: list[dict]) -> None:
    """Run morning-feed handoffs through the service layer."""
    for handoff in handoffs:
        owner_ref = str(handoff.get("owner_ref") or "")
        if not owner_ref:
            continue
        config = get_config(owner_ref)
        if not config.enabled:
            continue
        try:
            await run_morning_feed(config)
        except Exception:
            _log.exception("morning feed failed for %s", owner_ref)


def build_routine_dispatch() -> Callable[[list[dict]], Any]:
    """Compose the routine clock's dispatch: feed handoffs vs. the default.

    Plugs into ``RoutineClock(dispatch_fn=...)``. The feed path is a
    deterministic report, not agent work, so it does not go through
    GoalRun; every other routine is untouched.
    """
    from server.bot_team.routine_clock import _default_handoff_consumer

    async def dispatch(handoffs: list[dict]) -> None:
        feed = [h for h in handoffs if is_feed_routine(h.get("routine_id", ""))]
        rest = [h for h in handoffs if not is_feed_routine(h.get("routine_id", ""))]
        if feed:
            try:
                await _dispatch_feed(feed)
            except Exception:
                _log.exception("morning feed dispatch failed")
        if rest:
            await _default_handoff_consumer(rest)

    return dispatch


__all__ = [
    "FEED_ROUTINE_PREFIX",
    "MorningFeedConfig",
    "artifacts_dir",
    "build_routine_dispatch",
    "config_path",
    "due_configs",
    "gather_sections",
    "get_config",
    "is_feed_routine",
    "load_configs",
    "run_morning_feed",
    "save_config",
]
