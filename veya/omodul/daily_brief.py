"""veya/omodul/daily_brief.py — 晨间简报 (morning feed) omodul.

Assembles the per-user morning feed from already-gathered sections:

* today's routines / goals status
* recent GoalRun results
* bot memory highlights
* optional RSS / URL list

Driven by the existing routine clock (P4) — this module introduces no
scheduler and no task store.

3O contract (§5):
* signature ``(config, input_data, output_dir) -> dict`` with status/error
* ``_enabled_pillars`` declares ``report`` (the brief is the deliverable)
  and ``cost`` (the optional LLM summary is metered)
* never raises on failure; ``CancelledError`` is re-raised (§5.6 C4)
* no omodul -> omodul calls; it composes pure oskill formatters

The fingerprint only ever sees ``owner_ref``, the stable non-PII reference
produced by the service layer (SPEC §5.5.1).

Delivery (Web Push, channel post) is deliberately NOT done here: those are
credentialed side effects owned by the service layer, which consumes this
module's ``report_path``.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

from veya.oprim.event import emit_event
from veya.oskill.im.dingtalk import format_dingtalk_message
from veya.oskill.im.feishu import format_feishu_message
from veya.oskill.im.telegram import format_telegram_message
from veya.oskill.im.wecom import format_wecom_message

_log = logging.getLogger(__name__)

#: Sections the brief can include, in render order.
SECTIONS = ("routines", "goals", "goalruns", "memory", "rss")

#: Per-platform payload builders (oskill) used to pre-shape channel delivery.
_CHANNEL_FORMATTERS = {
    "telegram": format_telegram_message,
    "feishu": format_feishu_message,
    "dingtalk": format_dingtalk_message,
    "wecom": format_wecom_message,
}

_STATUS_ICONS = {
    "completed": "✅",
    "done": "✅",
    "failed": "❌",
    "error": "❌",
    "running": "⏳",
    "pending": "⏳",
}


@dataclass
class DailyBriefConfig:
    """Per-user morning-feed configuration (no real user id ever reaches here)."""

    owner_ref: str  # stable non-PII reference, from the service layer
    timezone: str = "Asia/Shanghai"
    brief_time: str = "08:00"  # HH:MM local
    sections: list[str] = field(default_factory=lambda: list(SECTIONS))
    max_items_per_section: int = 5
    title: str = "☀️ 晨间简报"
    # Platforms the service layer may deliver to; payloads are pre-shaped here.
    push_channels: list[str] = field(default_factory=list)

    # 3O metadata (§5.3): ClassVar, not instance state.
    _enabled_pillars: ClassVar[set[str]] = {"report", "cost"}
    _fingerprint_fields: ClassVar[set[str]] = {"owner_ref", "brief_time", "timezone"}

    def fingerprint(self) -> str:
        """Stable digest of the dedup-relevant config fields (§5.5)."""
        parts = [
            str(getattr(self, field_name, "")) for field_name in sorted(self._fingerprint_fields)
        ]
        return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:24]


@dataclass
class DailyBriefInput:
    """Already-gathered feed sections supplied by the service layer."""

    routines: list[dict[str, Any]] = field(default_factory=list)
    goals: list[dict[str, Any]] = field(default_factory=list)
    goalruns: list[dict[str, Any]] = field(default_factory=list)
    memory_highlights: list[dict[str, Any]] = field(default_factory=list)
    rss_items: list[dict[str, Any]] = field(default_factory=list)
    generated_at: float = field(default_factory=time.time)


async def daily_brief(
    config: DailyBriefConfig,
    input_data: DailyBriefInput,
    output_dir: Path,
    *,
    on_step: Any = None,
) -> dict[str, Any]:
    """Render the morning brief and persist it as a report artifact.

    Args:
        config: per-user feed configuration.
        input_data: pre-gathered sections (the omodul does no fetching).
        output_dir: directory the report artifact is written to.
        on_step: optional step callback for SSE progress.

    Returns:
        ``{status, error, findings, fingerprint, report_path, cost_usd}``.
    """
    fingerprint = config.fingerprint()
    cost_usd = 0.0
    started = time.time()

    try:
        markdown = _render(config, input_data)
        _emit(on_step, {"step": "rendered", "chars": len(markdown)})
        report_path = await asyncio.to_thread(_write_report, output_dir, fingerprint, markdown)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # §5.4: never raise on failure
        _log.exception("daily_brief failed")
        return {
            "status": "failed",
            "error": {"type": "render_failed", "message": str(exc)},
            "findings": None,
            "fingerprint": fingerprint,
            "report_path": None,
            "cost_usd": 0.0,
        }

    _emit(on_step, {"step": "reported", "path": str(report_path)})

    findings = {
        "markdown": markdown,
        "sections": list(config.sections),
        "empty": _empty_sections(input_data),
        "duration_s": time.time() - started,
    }

    # Pre-shape per-platform payloads so the service layer only has to hand
    # them to the obase client (chunking stays a pure oskill concern).
    channel_payloads = _channel_payloads(config, markdown)
    if channel_payloads:
        findings["channel_payloads"] = channel_payloads

    emit_event(
        "daily_brief.completed",
        {
            "owner_ref": config.owner_ref,
            "fingerprint": fingerprint,
            "sections": list(config.sections),
            "channels": sorted(channel_payloads),
            "duration_s": findings["duration_s"],
        },
    )

    return {
        "status": "completed",
        "error": None,
        "findings": findings,
        "fingerprint": fingerprint,
        "report_path": str(report_path),
        # cost pillar: no LLM call is made here, so the metered cost is 0.
        "cost_usd": cost_usd,
    }


def _channel_payloads(config: DailyBriefConfig, markdown: str) -> dict[str, list[Any]]:
    """Format the brief into send-ready payloads for each configured channel."""
    payloads: dict[str, list[Any]] = {}
    for platform in config.push_channels:
        formatter = _CHANNEL_FORMATTERS.get(platform)
        if formatter is None:
            continue
        payloads[platform] = formatter(markdown)
    return payloads


def compute_fingerprint_for(
    config: DailyBriefConfig,
    input_data: DailyBriefInput,
) -> str:
    """Expose the fingerprint for service-layer dedup (SPEC §5.5)."""
    return config.fingerprint()


# ---- internals ----------------------------------------------------------


def _write_report(output_dir: Path, fingerprint: str, markdown: str) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"daily_brief_{fingerprint}.md"
    path.write_text(markdown, encoding="utf-8")
    return path


def _emit(on_step: Any, payload: dict[str, Any]) -> None:
    if on_step is None:
        return
    try:
        on_step(payload)
    except Exception:  # progress must never break the transaction
        _log.debug("on_step callback failed", exc_info=True)


def _render(config: DailyBriefConfig, input_data: DailyBriefInput) -> str:
    """Compose the markdown brief from the enabled sections."""
    stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(input_data.generated_at))
    lines = [
        f"# {config.title} — {stamp}",
        f"_时区: {config.timezone} · 计划: {config.brief_time}_",
        "",
    ]

    for section in SECTIONS:
        if section not in config.sections:
            continue
        renderer = _RENDERERS[section]
        rendered = renderer(config, getattr(input_data, _INPUT_ATTRS[section]))
        if rendered:
            lines.extend(rendered)

    if len(lines) <= 3:
        lines.extend(["", "_今日暂无更新。_"])
    return "\n".join(lines).rstrip() + "\n"


_INPUT_ATTRS = {
    "routines": "routines",
    "goals": "goals",
    "goalruns": "goalruns",
    "memory": "memory_highlights",
    "rss": "rss_items",
}


def _items(config: DailyBriefConfig, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    limit = max(0, int(config.max_items_per_section))
    return items[:limit]


def _render_routines(config: DailyBriefConfig, routines: list[dict[str, Any]]) -> list[str]:
    rows = _items(config, routines)
    if not rows:
        return []
    out = ["## 📋 今日例行"]
    for row in rows:
        icon = "✅" if row.get("enabled") else "⏸️"
        goal = str(row.get("goal_template", ""))[:80]
        out.append(f"- {icon} {row.get('name', '未命名')}" + (f": {goal}" if goal else ""))
    out.append("")
    return out


def _render_goals(config: DailyBriefConfig, goals: list[dict[str, Any]]) -> list[str]:
    rows = _items(config, goals)
    if not rows:
        return []
    out = ["## 🎯 目标状态"]
    for row in rows:
        status = str(row.get("status", "pending"))
        out.append(f"- {_STATUS_ICONS.get(status, '•')} {row.get('title', '未命名')} — {status}")
    out.append("")
    return out


def _render_goalruns(config: DailyBriefConfig, goalruns: list[dict[str, Any]]) -> list[str]:
    rows = _items(config, goalruns)
    if not rows:
        return []
    out = ["## 🏃 最近执行"]
    for row in rows:
        status = str(row.get("status", ""))
        icon = _STATUS_ICONS.get(status, "•")
        started = str(row.get("started_at", ""))[:16]
        out.append(
            f"- {icon} {row.get('goal_id', 'unknown')}" + (f" ({started})" if started else "")
        )
    out.append("")
    return out


def _render_memory(config: DailyBriefConfig, highlights: list[dict[str, Any]]) -> list[str]:
    rows = _items(config, highlights)
    if not rows:
        return []
    out = ["## 🧠 记忆高光"]
    for row in rows:
        summary = str(row.get("summary") or row.get("text") or "").strip()
        if summary:
            out.append(f"- {summary[:100]}")
    if len(out) == 1:
        return []
    return [*out, ""]


def _render_rss(config: DailyBriefConfig, items: list[dict[str, Any]]) -> list[str]:
    rows = _items(config, items)
    if not rows:
        return []
    out = ["## 📰 订阅源"]
    for row in rows:
        title = str(row.get("title", "未命名"))
        link = str(row.get("link") or row.get("url") or "")
        out.append(f"- [{title}]({link})" if link else f"- {title}")
    out.append("")
    return out


_RENDERERS = {
    "routines": _render_routines,
    "goals": _render_goals,
    "goalruns": _render_goalruns,
    "memory": _render_memory,
    "rss": _render_rss,
}


def _empty_sections(input_data: DailyBriefInput) -> list[str]:
    """Which configured sections had no content (surfaced in findings)."""
    return [section for section in SECTIONS if not getattr(input_data, _INPUT_ATTRS[section])]


__all__ = [
    "SECTIONS",
    "DailyBriefConfig",
    "DailyBriefInput",
    "compute_fingerprint_for",
    "daily_brief",
]
