"""server/channels/dingtalk.py — DingTalk adapter (Layer 4)."""

from __future__ import annotations

import os
import time
from collections.abc import Mapping
from typing import Any

from veya.obase.channels import (
    PLATFORM_DINGTALK,
    ChannelMessage,
    verify_dingtalk_signature,
)

from .base import ChannelAdapter


class DingTalkAdapter(ChannelAdapter):
    """DingTalk adapter: outgoing-robot callback (HMAC signed)."""

    platform = PLATFORM_DINGTALK

    def __init__(
        self,
        *,
        app_key: str | None = None,
        app_secret: str | None = None,
        webhook_secret: str | None = None,
    ) -> None:
        self.app_key = app_key or os.environ.get("DINGTALK_APP_KEY", "")
        self.app_secret = app_secret or os.environ.get("DINGTALK_APP_SECRET", "")
        self.webhook_secret = webhook_secret or os.environ.get("DINGTALK_WEBHOOK_SECRET", "")

    # ---- verify ---------------------------------------------------------

    def verify(
        self, *, payload: Mapping[str, Any], headers: Mapping[str, str], query: Mapping[str, str]
    ) -> bool:
        """Verify the DingTalk outgoing-robot HMAC signature.

        DingTalk signs outbound deliveries with ``timestamp`` + ``sign`` and
        signs our replies with the same algorithm.
        """
        timestamp = str(payload.get("timestamp") or "")
        sign = str(payload.get("sign") or "")
        if not self.webhook_secret:
            return False
        if not verify_dingtalk_signature(
            webhook_secret=self.webhook_secret,
            timestamp=timestamp,
            sign=sign,
        ):
            return False
        # Replay guard: DingTalk considers callbacks older than an hour stale.
        try:
            sent_ms = int(timestamp)
        except (TypeError, ValueError):
            return False
        return abs(int(time.time() * 1000) - sent_ms) <= 3_600_000

    # ---- parse ----------------------------------------------------------

    def parse_inbound(
        self, *, payload: Mapping[str, Any], headers: Mapping[str, str]
    ) -> ChannelMessage | None:
        """Extract a text message from a DingTalk callback payload."""
        text = (payload.get("text") or {}).get("content")
        if not text:
            return None
        conversation = payload.get("conversationId")
        chat_id = conversation or payload.get("chatbotUserId")
        sender = (
            payload.get("senderStaffId")
            or payload.get("senderId")
            or payload.get("senderNick")
            or ""
        )
        if not chat_id:
            return None
        return ChannelMessage(
            platform=self.platform,
            external_chat_id=str(chat_id),
            external_user_id=str(sender or chat_id),
            text=str(text),
            raw_payload=dict(payload),
        )

    # ---- send -----------------------------------------------------------

    async def send_reply(self, client: Any, *, external_chat_id: str, text: str) -> dict[str, Any]:
        """Send a (chunked) reply via the obase DingTalk client."""
        results: list[dict[str, Any]] = []
        for chunk in ChannelAdapter.chunk_for_platform(text, platform=self.platform):
            results.append(await client.send_message(external_chat_id, chunk))
        return {"ok": True, "chunks": len(results), "results": results}


__all__ = ["DingTalkAdapter"]
