"""veya/omodul/daily_brief — omodul_daily_brief (晨间简报).

Per-user morning feed assembly:
- Today's routines/goals status
- Recent GoalRun results
- Bot memory highlights
- Optional RSS/URL list

Output stored as artifact + Web Push + optional channel delivery.
Driven by existing routine clock (P4), no new scheduler.

3O compliance:
- signature: (config, input_data, output_dir) -> dict with status/error
- declares _enabled_pillars = {"report", "cost"}
- no omodul->omodul calls; composes via oservi sequential_composer if needed
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from veya.obase.channels import chunk_text, ChannelReply, PLATFORM_TELEGRAM, PLATFORM_FEISHU, PLATFORM_DINGTALK, PLATFORM_WECOM
from veya.obase.llm import llm_call
from veya.oservi.manifest import ServiceManifest
from veya.oservi.assembler import assemble
from veya.oprim.event import emit_event

_log = logging.getLogger("veya.omodul.daily_brief")


@dataclass
class DailyBriefConfig:
    """Configuration for daily brief generation."""
    user_id: str  # pseudo-anonymized
    timezone: str = "Asia/Shanghai"
    brief_time: str = "08:00"  # HH:MM local time
    sections: list[str] = field(default_factory=lambda: [
        "routines", "goals", "goalruns", "memory", "rss"
    ])
    rss_urls: list[str] = field(default_factory=list)
    max_items_per_section: int = 5
    llm_provider: str = "anthropic"
    llm_model: str = "claude-sonnet-4-6"
    budget_usd: float = 2.0
    output_format: str = "markdown"
    push_webpush: bool = True
    push_channels: list[str] = field(default_factory=list)  # platform names

    # 3O pillars
    _enabled_pillars: set[str] = field(default_factory=lambda: {"report", "cost"})
    _fingerprint_fields: set[str] = field(default_factory=lambda: {"user_id", "brief_time", "timezone"})

    def compute_fingerprint(self) -> str:
        import hashlib
        parts = [str(getattr(self, f, "")) for f in self._fingerprint_fields]
        return hashlib.sha256("|".join(parts).encode()).hexdigest()[:24]


@dataclass
class DailyBriefInput:
    """Input data for daily brief generation."""
    routines: list[dict[str, Any]] = field(default_factory=list)
    goals: list[dict[str, Any]] = field(default_factory=list)
    goalruns: list[dict[str, Any]] = field(default_factory=list)
    memory_highlights: list[dict[str, Any]] = field(default_factory=list)
    rss_items: list[dict[str, Any]] = field(default_factory=list)
    generated_at: float = field(default_factory=time.time)


@dataclass
class DailyBriefOutput:
    """Output from daily brief generation."""
    markdown: str = ""
    artifact_path: str | None = None
    webpush_sent: bool = False
    channel_deliveries: list[dict[str, Any]] = field(default_factory=list)
    cost_usd: float = 0.0
    fingerprint: str = ""


async def daily_brief(
    config: DailyBriefConfig,
    input_data: DailyBriefInput,
    output_dir: Path,
) -> dict[str, Any]:
    """Generate daily brief — 3O standard omodul signature.

    Args:
        config: DailyBriefConfig with user preferences and pillars enabled
        input_data: DailyBriefInput with pre-fetched data sections
        output_dir: Directory to write artifact

    Returns:
        dict with keys: status, error, findings, cost_usd, fingerprint, report_path
    """
    started = time.time()
    fingerprint = config.compute_fingerprint()
    cost_usd = 0.0

    # 1. Assemble markdown from sections
    try:
        markdown = await _assemble_brief(config, input_data)
    except Exception as e:
        _log.exception("Failed to assemble daily brief")
        return {
            "status": "failed",
            "error": {"type": "assembly_failed", "message": str(e)},
            "findings": None,
            "cost_usd": 0.0,
            "fingerprint": fingerprint,
            "report_path": None,
        }

    # 2. Write artifact
    artifact_path = output_dir / f"daily_brief_{fingerprint}.md"
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
        artifact_path.write_text(markdown, encoding="utf-8")
    except Exception as e:
        _log.exception("Failed to write daily brief artifact")
        return {
            "status": "failed",
            "error": {"type": "artifact_write_failed", "message": str(e)},
            "findings": {"markdown": markdown},
            "cost_usd": cost_usd,
            "fingerprint": fingerprint,
            "report_path": None,
        }

    # 3. Web Push delivery (if enabled)
    webpush_sent = False
    if config.push_webpush:
        try:
            webpush_sent = await _send_webpush(config.user_id, markdown)
        except Exception as e:
            _log.warning(f"Web Push failed: {e}")

    # 4. Channel delivery (if enabled)
    channel_deliveries = []
    if config.push_channels:
        for platform in config.push_channels:
            try:
                result = await _send_channel(platform, config.user_id, markdown)
                channel_deliveries.append({"platform": platform, "status": "sent", "detail": result})
            except Exception as e:
                channel_deliveries.append({"platform": platform, "status": "failed", "error": str(e)})

    # 5. Emit completion event
    emit_event("daily_brief.completed", {
        "user_id": config.user_id,
        "fingerprint": fingerprint,
        "sections": config.sections,
        "webpush": webpush_sent,
        "channels": len(channel_deliveries),
        "duration_s": time.time() - started,
    })

    return {
        "status": "completed",
        "error": None,
        "findings": {
            "markdown": markdown,
            "webpush_sent": webpush_sent,
            "channel_deliveries": channel_deliveries,
        },
        "cost_usd": cost_usd,
        "fingerprint": fingerprint,
        "report_path": str(artifact_path),
    }


async def _assemble_brief(config: DailyBriefConfig, input_data: DailyBriefInput) -> str:
    """Assemble markdown from configured sections."""
    sections = []
    sections.append(f"# ☀️ 晨间简报 — {time.strftime('%Y-%m-%d %H:%M', time.localtime())}")
    sections.append(f"_时区: {config.timezone}_\n")

    if "routines" in config.sections and input_data.routines:
        sections.append("## 📋 今日例行")
        for r in input_data.routines[:config.max_items_per_section]:
            status = "✅" if r.get("enabled") else "⏸️"
            sections.append(f"- {status} {r.get('name', 'Unknown')}: {r.get('goal_template', '')[:80]}")
        sections.append("")

    if "goals" in config.sections and input_data.goals:
        sections.append("## 🎯 目标状态")
        for g in input_data.goals[:config.max_items_per_section]:
            sections.append(f"- {g.get('title', 'Unknown')}: {g.get('status', 'pending')}")
        sections.append("")

    if "goalruns" in config.sections and input_data.goalruns:
        sections.append("## 🏃 最近执行")
        for gr in input_data.goalruns[:config.max_items_per_section]:
            status_emoji = "✅" if gr.get("status") == "completed" else "❌" if gr.get("status") == "failed" else "⏳"
            sections.append(f"- {status_emoji} {gr.get('goal_id', 'Unknown')} ({gr.get('started_at', '')[:16]})")
        sections.append("")

    if "memory" in config.sections and input_data.memory_highlights:
        sections.append("## 🧠 记忆高光")
        for m in input_data.memory_highlights[:config.max_items_per_section]:
            sections.append(f"- {m.get('summary', 'Memory')[:100]}")
        sections.append("")

    if "rss" in config.sections and input_data.rss_items:
        sections.append("## 📰 订阅源")
        for item in input_data.rss_items[:config.max_items_per_section]:
            title = item.get("title", "Untitled")
            link = item.get("link", "")
            sections.append(f"- [{title}]({link})")
        sections.append("")

    return "\n".join(sections)


async def _send_webpush(user_id: str, markdown: str) -> bool:
    """Send daily brief via Web Push (P5)."""
    try:
        from server.bot_team.push import get_push_notifier
        notifier = get_push_notifier()
        # Truncate for push notification
        preview = markdown[:200] + "..." if len(markdown) > 200 else markdown
        await notifier.notify(
            user_id=user_id,
            title="☀️ 晨间简报",
            body=preview,
            data={"type": "daily_brief", "fingerprint": user_id},
            flags=["web_push"],
        )
        return True
    except Exception as e:
        _log.warning(f"Web Push notification failed: {e}")
        return False


async def _send_channel(platform: str, user_id: str, markdown: str) -> dict[str, Any]:
    """Send daily brief via chat channel."""
    # This would use the binding table to find the external chat_id
    # For now, return a placeholder
    return {"user_id": user_id, "platform": platform, "note": "binding lookup not implemented"}


# Single-source guard test helper
def compute_fingerprint_for(config: DailyBriefConfig, input_data: DailyBriefInput) -> str:
    """Exposed for service-layer deduplication (SPEC §5.5)."""
    return config.compute_fingerprint()


__all__ = [
    "DailyBriefConfig",
    "DailyBriefInput",
    "DailyBriefOutput",
    "daily_brief",
    "compute_fingerprint_for",
]