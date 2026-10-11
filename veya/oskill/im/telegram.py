"""veya/oskill/im/telegram.py — Pure Telegram message formatting (oskill).

≥2 oprims, stateless, no IO/persistence, no user_id, no framework import.

Composes the shared obase chunking oprim with the escaping/formatting oprims
below. All Telegram HTTP lives in ``veya.obase.channels.telegram``.
"""

from __future__ import annotations

import re
from typing import Any

from veya.obase.channels import PLATFORM_TELEGRAM, chunk_text

TELEGRAM_MAX_LEN = 4096

# MarkdownV2 reserved characters (Telegram Bot API).
_MD_V2_SPECIALS = r"_*[]()~`>#+-=|{}.!"


# ---- Formatting oprims ----


def escape_markdown(text: str) -> str:
    """Escape Telegram MarkdownV2 reserved characters."""
    return re.sub(f"([{re.escape(_MD_V2_SPECIALS)}])", r"\\\1", text)


def escape_html(text: str) -> str:
    """Escape Telegram HTML-mode reserved characters."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def format_telegram_code_block(code: str, *, language: str = "") -> str:
    """Wrap ``code`` in a MarkdownV2 fenced code block."""
    fence = "```"
    return f"{fence}{escape_markdown(language)}\n{escape_markdown(code)}\n{fence}"


def format_telegram_mention(username: str, *, user_id: int | str | None = None) -> str:
    """Build a MarkdownV2 mention link for a Telegram user."""
    label = escape_markdown(f"@{username}")
    if user_id is None:
        return label
    return f"[{label}](tg://user?id={user_id})"


# ---- Composed formatters ----


def format_telegram_message(text: str, *, max_len: int = TELEGRAM_MAX_LEN) -> list[dict[str, Any]]:
    """Format and chunk a message for Telegram.

    Oskill: composes ``chunk_text`` (obase oprim) with platform sizing.
    Stateless, no IO. Returns message payloads ready for ``sendMessage``.
    """
    return [
        {"text": chunk} for chunk in chunk_text(text, platform=PLATFORM_TELEGRAM, max_len=max_len)
    ]


def format_task_started(username: str, task_preview: str) -> dict[str, Any]:
    """Format the 'task started' acknowledgement."""
    return {"text": (f"🚀 *{username}* started a task:\n> {task_preview[:200]}\n⏳ Processing...")}


def format_task_result(
    username: str,
    task_preview: str,
    result: str,
    cost_usd: float,
) -> list[dict[str, Any]]:
    """Format a completed task result, chunked for Telegram."""
    text = f"*{username}* asked:\n> {task_preview[:200]}\n\n{result}\n\n💰 ${cost_usd:.4f}"
    return format_telegram_message(text)


def format_error(username: str, error: str) -> dict[str, Any]:
    """Format an error notification."""
    return {"text": f"❌ *{username}* — Error: {error[:200]}"}


def format_status(username: str, session_active: bool, pseudo_id: str) -> dict[str, Any]:
    """Format a status response."""
    state = "✅ Active" if session_active else "⏸ Inactive"
    return {
        "text": (
            "🤖 *veya* status\n\n"
            f"• Session: {state}\n"
            f"• User: {username} ({pseudo_id})\n"
            "• Gateway: Telegram"
        )
    }


def format_help() -> dict[str, Any]:
    """Format the help message."""
    return {
        "text": (
            "*veya Help*\n\n"
            "• Send any message to start a task\n"
            "• /status — check bot status\n"
            "• /history — view recent tasks\n"
            "• /reset — clear conversation context"
        )
    }


__all__ = [
    "TELEGRAM_MAX_LEN",
    "escape_html",
    "escape_markdown",
    "format_error",
    "format_help",
    "format_status",
    "format_task_result",
    "format_task_started",
    "format_telegram_code_block",
    "format_telegram_mention",
    "format_telegram_message",
]
