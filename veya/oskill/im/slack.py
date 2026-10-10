"""veya/oskill/im/slack.py — Pure Slack message formatting (oskill).

≥2 oprims, stateless, no IO/persistence, no user_id.
"""

from __future__ import annotations

import re
from typing import Any

from veya.obase.channels import chunk_text

# Slack doesn't have a platform constant in obase.channels yet
SLACK_MAX_LEN = 3900

# ---- Formatting oprims ----


def escape_slack_text(text: str) -> str:
    """Escape text for Slack mrkdwn.

    Oprim: pure string transformation.
    """
    # Slack mrkdwn special chars: * _ ~ ` >
    special = r"*_~`>"
    return re.sub(f"([{re.escape(special)}])", r"\\\1", text)


def format_slack_text_message(text: str) -> dict[str, Any]:
    """Format a plain text message for Slack.

    Oprim: pure data transformation.
    """
    return {"type": "mrkdwn", "text": text}


def format_slack_block_message(blocks: list[dict[str, Any]]) -> dict[str, Any]:
    """Format a Block Kit message for Slack."""
    return {"blocks": blocks}


def build_slack_section(text: str) -> dict[str, Any]:
    """Build a section block for Slack Block Kit."""
    return {"type": "section", "text": format_slack_text_message(text)}


def build_slack_context(elements: list[dict[str, Any]]) -> dict[str, Any]:
    """Build a context block for Slack Block Kit."""
    return {"type": "context", "elements": elements}


# ---- Composed formatters ----


def format_slack_message(
    text: str,
    *,
    max_len: int = SLACK_MAX_LEN,
) -> list[dict[str, Any]]:
    """Format and chunk a message for Slack.

    Oskill: composes escape + chunk oprims. Stateless, no IO.

    Returns list of message dicts ready to send.
    """
    chunks = chunk_text(text, platform="slack", max_len=max_len)

    results = []
    for chunk in chunks:
        results.append({"text": chunk})
    return results


def format_task_started(username: str, task_preview: str) -> dict[str, Any]:
    """Format 'task started' notification."""
    text = f"🚀 *{username}* started a task:\n> _{task_preview[:200]}..._\n⏳ Processing..."
    return {"text": text}


def format_task_result(
    username: str,
    task_preview: str,
    result: str,
    cost_usd: float,
) -> list[dict[str, Any]]:
    """Format task completion."""
    text = f"*{username}* asked:\n> _{task_preview[:200]}..._\n\n{result}\n\n💰 ${cost_usd:.4f}"
    return format_slack_message(text)


def format_error(username: str, error: str) -> dict[str, Any]:
    """Format error notification."""
    return {"text": f"❌ *{username}* — Error: _{error[:200]}_"}


def format_status(username: str, session_active: bool, pseudo_id: str) -> dict[str, Any]:
    """Format status response."""
    status = "✅ Active" if session_active else "⏸ Inactive"
    text = (
        f"🤖 *veya* status\n\n"
        f"• Session: {status}\n"
        f"• User: {username} ({pseudo_id})\n"
        f"• Gateway: Slack"
    )
    return {"text": text}


def format_help() -> dict[str, Any]:
    """Format help message."""
    text = (
        "*veya Help*\n\n"
        "• Send any message to start a task\n"
        "• /status — check bot status\n"
        "• /history — view recent tasks\n"
        "• /reset — clear conversation context"
    )
    return {"text": text}


__all__ = [
    "build_slack_context",
    "build_slack_section",
    "escape_slack_text",
    "format_error",
    "format_help",
    "format_slack_block_message",
    "format_slack_message",
    "format_slack_text_message",
    "format_status",
    "format_task_result",
    "format_task_started",
]
