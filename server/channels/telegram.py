"""server/channels/telegram.py — Telegram adapter (Layer 4)."""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any

from veya.obase.channels import PLATFORM_TELEGRAM, ChannelMessage, verify_telegram_signature

from .base import ChannelAdapter


class TelegramAdapter(ChannelAdapter):
    """Telegram Bot API adapter (webhook mode).

    Secrets are read from the environment only; nothing is stored in the repo.
    """

    platform = PLATFORM_TELEGRAM

    def __init__(self, *, bot_token: str | None = None, secret_token: str | None = None) -> None:
        self.bot_token = bot_token or os.environ.get("TELEGRAM_BOT_TOKEN", "")
        self.secret_token = secret_token or os.environ.get("TELEGRAM_SECRET_TOKEN", "")

    # ---- verify ---------------------------------------------------------

    def verify(
        self, *, payload: Mapping[str, Any], headers: Mapping[str, str], query: Mapping[str, str]
    ) -> bool:
        """Check the ``X-Telegram-Bot-Api-Secret-Token`` header."""
        return verify_telegram_signature(
            secret_token=self.secret_token or None,
            provided=headers.get("x-telegram-bot-api-secret-token"),
        )

    # ---- parse ----------------------------------------------------------

    def parse_inbound(
        self, *, payload: Mapping[str, Any], headers: Mapping[str, str]
    ) -> ChannelMessage | None:
        """Extract a text message from a Telegram ``Update``."""
        message = payload.get("message") or payload.get("edited_message")
        if not isinstance(message, dict):
            return None
        text = message.get("text")
        if not text:
            return None
        chat = message.get("chat") or {}
        sender = message.get("from") or {}
        chat_id = chat.get("id")
        if chat_id is None:
            return None
        return ChannelMessage(
            platform=self.platform,
            external_chat_id=str(chat_id),
            external_user_id=str(sender.get("id") or chat_id),
            text=str(text),
            raw_payload=dict(message),
        )

    # ---- send -----------------------------------------------------------

    async def send_reply(self, client: Any, *, external_chat_id: str, text: str) -> dict[str, Any]:
        """Send a (chunked) reply via the obase Telegram client."""
        results: list[dict[str, Any]] = []
        for chunk in chunk_text_for_platform(text):
            results.append(await client.send_message(external_chat_id, chunk))
        return {"ok": True, "chunks": len(results), "results": results}


def chunk_text_for_platform(text: str) -> list[str]:
    """Long replies are split to Telegram's 4096-character limit."""
    return ChannelAdapter.chunk_for_platform(text, platform=PLATFORM_TELEGRAM)


__all__ = ["TelegramAdapter"]
