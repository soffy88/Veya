"""veya/oskill/im/dingtalk.py — Pure DingTalk message formatting (oskill).

≥2 oprims, stateless, no IO/persistence, no user_id, no framework import.

DingTalk payloads are ``{"msgtype": ..., "<msgtype>": {...}}`` envelopes; the
formatters here build them. All DingTalk HTTP/HMAC lives in
``veya.obase.channels.dingtalk``.
"""

from __future__ import annotations

import re
from typing import Any

from veya.obase.channels import PLATFORM_DINGTALK, chunk_text

DINGTALK_MAX_LEN = 5000

# DingTalk markdown headings must be preceded by a blank line.
_HEADING_RE = re.compile(r"(?m)^(#{1,6}\s.*)$")


# ---- Formatting oprims ----


def escape_dingtalk_markdown(text: str) -> str:
    """Escape characters that would otherwise break DingTalk markdown."""
    return text.replace("@", "\u2005").replace("<", "&lt;").replace(">", "&gt;")


def _normalize_headings(text: str) -> str:
    """Ensure a blank line before every ATX heading (DingTalk requirement)."""
    return _HEADING_RE.sub(lambda m: f"\n{m.group(1)}", text).lstrip("\n")


def format_dingtalk_text_message(text: str) -> dict[str, Any]:
    """Build a DingTalk ``text`` message."""
    return {"msgtype": "text", "text": {"content": text}}


def format_dingtalk_markdown_message(text: str, *, title: str = "") -> dict[str, Any]:
    """Build a DingTalk ``markdown`` message."""
    return {
        "msgtype": "markdown",
        "markdown": {"title": title, "text": _normalize_headings(text)},
    }


def format_dingtalk_link_message(
    text: str,
    title: str,
    message_url: str,
    pic_url: str = "",
) -> dict[str, Any]:
    """Build a DingTalk ``link`` message."""
    return {
        "msgtype": "link",
        "link": {
            "text": text,
            "title": title,
            "picUrl": pic_url,
            "messageUrl": message_url,
        },
    }


# ---- Composed formatters ----


def format_dingtalk_message(
    text: str,
    *,
    max_len: int = DINGTALK_MAX_LEN,
    markdown: bool = True,
) -> list[dict[str, Any]]:
    """Format and chunk a message for DingTalk.

    Oskill: composes ``chunk_text`` (obase oprim) with envelope shaping.
    Stateless, no IO.
    """
    build = format_dingtalk_markdown_message if markdown else format_dingtalk_text_message
    return [build(chunk) for chunk in chunk_text(text, platform=PLATFORM_DINGTALK, max_len=max_len)]


def format_task_started(username: str, task_preview: str) -> dict[str, Any]:
    """Format the 'task started' acknowledgement."""
    return format_dingtalk_markdown_message(
        f"### 🚀 {username} started a task\n\n> {task_preview[:200]}\n\n⏳ Processing..."
    )


def format_task_result(
    username: str,
    task_preview: str,
    result: str,
    cost_usd: float,
) -> list[dict[str, Any]]:
    """Format a completed task result, chunked for DingTalk."""
    text = f"### {username} asked\n\n> {task_preview[:200]}\n\n{result}\n\n💰 ${cost_usd:.4f}"
    return format_dingtalk_message(text)


def format_error(username: str, error: str) -> dict[str, Any]:
    """Format an error notification."""
    return format_dingtalk_markdown_message(f"### ❌ {username} — Error\n\n{error[:200]}")


def format_status(username: str, session_active: bool, pseudo_id: str) -> dict[str, Any]:
    """Format a status response."""
    state = "✅ Active" if session_active else "⏸ Inactive"
    return format_dingtalk_markdown_message(
        "### 🤖 veya status\n\n"
        f"- Session: {state}\n"
        f"- User: {username} ({pseudo_id})\n"
        "- Gateway: DingTalk"
    )


def format_help() -> dict[str, Any]:
    """Format the help message."""
    return format_dingtalk_markdown_message(
        "### veya Help\n\n"
        "- Send any message to start a task\n"
        "- /status — check bot status\n"
        "- /history — view recent tasks\n"
        "- /reset — clear conversation context"
    )


__all__ = [
    "DINGTALK_MAX_LEN",
    "escape_dingtalk_markdown",
    "format_dingtalk_link_message",
    "format_dingtalk_markdown_message",
    "format_dingtalk_message",
    "format_dingtalk_text_message",
    "format_error",
    "format_help",
    "format_status",
    "format_task_result",
    "format_task_started",
]
