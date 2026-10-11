"""veya/oskill/im/feishu.py — Pure Feishu/Lark message formatting (oskill).

≥2 oprims, stateless, no IO/persistence, no user_id, no framework import.

Feishu ``post`` content is a structured JSON document, so the formatters here
build that document; all Feishu HTTP lives in ``veya.obase.channels.feishu``.
"""

from __future__ import annotations

from typing import Any

from veya.obase.channels import PLATFORM_FEISHU, chunk_text

FEISHU_MAX_LEN = 1900


# ---- Block oprims ----


def build_text_block(text: str) -> dict[str, Any]:
    """Build a Feishu rich-text ``text`` element."""
    return {"tag": "text", "text": text}


def build_at_block(user_id: str) -> dict[str, Any]:
    """Build a Feishu rich-text ``at`` (mention) element."""
    return {"tag": "at", "user_id": user_id}


def build_link_block(text: str, href: str) -> dict[str, Any]:
    """Build a Feishu rich-text ``a`` (hyperlink) element."""
    return {"tag": "a", "text": text, "href": href}


def escape_feishu_text(text: str) -> str:
    """Normalise whitespace/control characters for Feishu rich text."""
    return "".join(ch for ch in text if ch == "\n" or ch >= " ")


# ---- Composed formatters ----


def format_feishu_post_content(lines: list[dict[str, Any]], *, title: str = "") -> dict[str, Any]:
    """Wrap rich-text element rows into Feishu ``post`` content."""
    body: dict[str, Any] = {"title": title, "content": [lines]}
    return {"zh_cn": body}


def format_feishu_message(
    text: str,
    *,
    max_len: int = FEISHU_MAX_LEN,
    title: str = "",
) -> list[dict[str, Any]]:
    """Format and chunk a message for Feishu.

    Oskill: composes ``chunk_text`` (obase oprim) with ``post`` shaping.
    Stateless, no IO.
    """
    return [
        format_feishu_post_content([[build_text_block(chunk)]], title=title)
        for chunk in chunk_text(text, platform=PLATFORM_FEISHU, max_len=max_len)
    ]


def format_task_started(username: str, task_preview: str) -> dict[str, Any]:
    """Format the 'task started' acknowledgement."""
    return format_feishu_post_content(
        [
            [
                build_text_block(f"🚀 {username} started a task:"),
                build_text_block(f"> {task_preview[:200]}"),
                build_text_block("⏳ Processing..."),
            ]
        ]
    )


def format_task_result(
    username: str,
    task_preview: str,
    result: str,
    cost_usd: float,
) -> list[dict[str, Any]]:
    """Format a completed task result, chunked for Feishu."""
    text = f"{username} asked:\n> {task_preview[:200]}\n\n{result}\n\n💰 ${cost_usd:.4f}"
    return format_feishu_message(text)


def format_error(username: str, error: str) -> dict[str, Any]:
    """Format an error notification."""
    return format_feishu_post_content([[build_text_block(f"❌ {username} — Error: {error[:200]}")]])


def format_status(username: str, session_active: bool, pseudo_id: str) -> dict[str, Any]:
    """Format a status response."""
    state = "✅ Active" if session_active else "⏸ Inactive"
    return format_feishu_post_content(
        [
            [
                build_text_block("🤖 veya status"),
                build_text_block(f"• Session: {state}"),
                build_text_block(f"• User: {username} ({pseudo_id})"),
                build_text_block("• Gateway: Feishu"),
            ]
        ]
    )


def format_help() -> dict[str, Any]:
    """Format the help message."""
    return format_feishu_post_content(
        [
            [
                build_text_block("veya Help"),
                build_text_block("• Send any message to start a task"),
                build_text_block("• /status — check bot status"),
                build_text_block("• /history — view recent tasks"),
                build_text_block("• /reset — clear conversation context"),
            ]
        ]
    )


__all__ = [
    "FEISHU_MAX_LEN",
    "build_at_block",
    "build_link_block",
    "build_text_block",
    "escape_feishu_text",
    "format_error",
    "format_feishu_message",
    "format_feishu_post_content",
    "format_help",
    "format_status",
    "format_task_result",
    "format_task_started",
]
