"""veya/oskill/im/wecom.py — Pure WeCom (Enterprise WeChat) message formatting (oskill).

≥2 oprims, stateless, no IO/persistence, no user_id, no framework import.

Two message shapes are handled here, both as pure formatting:
- the JSON ``markdown`` payload sent through the message/customer-service API
- the XML callback reply returned synchronously to the WeCom callback server

All WeCom HTTP, AES msg-crypto and signature verification live in
``veya.obase.channels.wecom``.
"""

from __future__ import annotations

import time
from typing import Any

from veya.obase.channels import PLATFORM_WECOM, chunk_text

WECOM_MAX_LEN = 2048


# ---- Formatting oprims ----


def escape_wecom_markdown(text: str) -> str:
    """Escape text for the WeCom markdown message type.

    ``@`` is escaped with a zero-width space so usernames are not turned into
    mentions, and angle brackets are entity-escaped.
    """
    return text.replace("@", "\u2005").replace("<", "&lt;").replace(">", "&gt;")


def format_wecom_text_message(text: str) -> dict[str, Any]:
    """Build a WeCom ``text`` message."""
    return {"msgtype": "text", "text": {"content": text}}


def format_wecom_markdown_message(text: str) -> dict[str, Any]:
    """Build a WeCom ``markdown`` message."""
    return {"msgtype": "markdown", "markdown": {"content": text}}


def format_wecom_news_message(
    articles: list[dict[str, str]],
) -> dict[str, Any]:
    """Build a WeCom ``news`` message from ``{title, description, url, picurl}`` items."""
    return {"msgtype": "news", "news": {"articles": list(articles)}}


def format_wecom_text_reply_xml(
    to_user: str,
    from_user: str,
    content: str,
    *,
    created_at: int | None = None,
) -> str:
    """Build the synchronous XML text reply returned to the WeCom callback server."""
    stamp = int(created_at if created_at is not None else time.time())
    return (
        "<xml>\n"
        f"<ToUserName><![CDATA[{to_user}]]></ToUserName>\n"
        f"<FromUserName><![CDATA[{from_user}]]></FromUserName>\n"
        f"<CreateTime>{stamp}</CreateTime>\n"
        "<MsgType><![CDATA[text]]></MsgType>\n"
        f"<Content><![CDATA[{content}]]></Content>\n"
        "</xml>"
    )


# ---- Composed formatters ----


def format_wecom_message(
    text: str,
    *,
    max_len: int = WECOM_MAX_LEN,
    msgtype: str = "markdown",
) -> list[dict[str, Any]]:
    """Format and chunk a message for WeCom.

    Oskill: composes ``chunk_text`` (obase oprim) with envelope shaping.
    Stateless, no IO.
    """
    build = format_wecom_markdown_message if msgtype == "markdown" else format_wecom_text_message
    return [build(chunk) for chunk in chunk_text(text, platform=PLATFORM_WECOM, max_len=max_len)]


def format_task_started(username: str, task_preview: str) -> dict[str, Any]:
    """Format the 'task started' acknowledgement."""
    return format_wecom_markdown_message(
        f"### 🚀 {username} started a task\n\n> {task_preview[:200]}\n\n⏳ Processing..."
    )


def format_task_result(
    username: str,
    task_preview: str,
    result: str,
    cost_usd: float,
) -> list[dict[str, Any]]:
    """Format a completed task result, chunked for WeCom."""
    text = f"### {username} asked\n\n> {task_preview[:200]}\n\n{result}\n\n💰 ${cost_usd:.4f}"
    return format_wecom_message(text)


def format_error(username: str, error: str) -> dict[str, Any]:
    """Format an error notification."""
    return format_wecom_markdown_message(f"### ❌ {username} — Error\n\n{error[:200]}")


def format_status(username: str, session_active: bool, pseudo_id: str) -> dict[str, Any]:
    """Format a status response."""
    state = "✅ Active" if session_active else "⏸ Inactive"
    return format_wecom_markdown_message(
        "### 🤖 veya status\n\n"
        f"- Session: {state}\n"
        f"- User: {username} ({pseudo_id})\n"
        "- Gateway: WeCom"
    )


def format_help() -> dict[str, Any]:
    """Format the help message."""
    return format_wecom_markdown_message(
        "### veya Help\n\n"
        "- Send any message to start a task\n"
        "- /status — check bot status\n"
        "- /history — view recent tasks\n"
        "- /reset — clear conversation context"
    )


__all__ = [
    "WECOM_MAX_LEN",
    "escape_wecom_markdown",
    "format_error",
    "format_help",
    "format_status",
    "format_task_result",
    "format_task_started",
    "format_wecom_markdown_message",
    "format_wecom_message",
    "format_wecom_news_message",
    "format_wecom_text_message",
    "format_wecom_text_reply_xml",
]
