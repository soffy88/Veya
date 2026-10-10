"""server/channels/feishu.py — Feishu/Lark adapter (Layer 4)."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from typing import Any

from veya.obase.channels import PLATFORM_FEISHU, ChannelMessage, verify_feishu_signature

from .base import ChannelAdapter


class FeishuAdapter(ChannelAdapter):
    """Feishu/Lark event-subscription adapter.

    Handles the ``url_verification`` handshake and text messages; the
    verification token / encrypt key come from the environment only.
    """

    platform = PLATFORM_FEISHU

    def __init__(
        self,
        *,
        app_id: str | None = None,
        app_secret: str | None = None,
        encrypt_key: str | None = None,
    ) -> None:
        self.app_id = app_id or os.environ.get("FEISHU_APP_ID", "")
        self.app_secret = app_secret or os.environ.get("FEISHU_APP_SECRET", "")
        self.encrypt_key = encrypt_key or os.environ.get("FEISHU_ENCRYPT_KEY", "")

    # ---- verify ---------------------------------------------------------

    def verify(
        self, *, payload: Mapping[str, Any], headers: Mapping[str, str], query: Mapping[str, str]
    ) -> bool:
        """Check the ``X-Lark-Signature`` header when the platform sends one.

        The subscription handshake and the v1 verification-token flow do not
        carry a signature; those are handled by :meth:`challenge_response`.
        """
        signature = headers.get("x-lark-signature")
        timestamp = headers.get("x-lark-request-timestamp")
        nonce = headers.get("x-lark-request-nonce")
        if not (signature and timestamp and nonce):
            return self._verify_token(payload)
        return verify_feishu_signature(
            encrypt_key=self.encrypt_key,
            timestamp=timestamp,
            nonce=nonce,
            signature=signature,
        )

    def _verify_token(self, payload: Mapping[str, Any]) -> bool:
        """Verification-token flow: header token must equal the configured one."""
        token = payload.get("token") or (payload.get("header") or {}).get("token")
        expected = os.environ.get("FEISHU_VERIFICATION_TOKEN", "")
        return bool(expected) and bool(token) and token == expected

    def challenge_response(self, payload: Mapping[str, Any]) -> dict[str, Any] | None:
        """Return the ``url_verification`` echo body, or None."""
        if payload.get("type") == "url_verification" or "challenge" in payload:
            return {"challenge": payload.get("challenge", "")}
        return None

    # ---- parse ----------------------------------------------------------

    def parse_inbound(
        self, *, payload: Mapping[str, Any], headers: Mapping[str, str]
    ) -> ChannelMessage | None:
        """Extract a text message from a Feishu event subscription payload."""
        event = payload.get("event") or payload.get("data")
        if not isinstance(event, dict):
            return None
        message = event.get("message")
        if not isinstance(message, dict):
            return None

        content = message.get("content") or ""
        text = content
        if isinstance(content, str):
            try:
                parsed = json.loads(content)
                text = parsed.get("text", "") if isinstance(parsed, dict) else content
            except json.JSONDecodeError:
                text = content
        if not text:
            return None

        chat_id = message.get("chat_id") or event.get("chat_id")
        sender = event.get("sender") or {}
        sender_id = sender.get("sender_id") if isinstance(sender, dict) else None
        if isinstance(sender_id, dict):
            sender_id = sender_id.get("open_id") or sender_id.get("user_id")
        if not chat_id:
            return None
        return ChannelMessage(
            platform=self.platform,
            external_chat_id=str(chat_id),
            external_user_id=str(sender_id or chat_id),
            text=str(text),
            raw_payload=dict(message),
        )

    # ---- send -----------------------------------------------------------

    async def send_reply(self, client: Any, *, external_chat_id: str, text: str) -> dict[str, Any]:
        """Send a (chunked) reply via the obase Feishu client."""
        results: list[dict[str, Any]] = []
        for chunk in ChannelAdapter.chunk_for_platform(text, platform=self.platform):
            results.append(await client.send_message(external_chat_id, chunk))
        return {"ok": True, "chunks": len(results), "results": results}


__all__ = ["FeishuAdapter"]
