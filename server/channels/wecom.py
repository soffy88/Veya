"""server/channels/wecom.py — WeCom (Enterprise WeChat) adapter (Layer 4)."""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any

from veya.obase.channels import (
    PLATFORM_WECOM,
    ChannelMessage,
    verify_wecom_signature,
    verify_wecom_url_signature,
)
from veya.obase.channels.safe_xml import ET

from .base import ChannelAdapter


class WeComAdapter(ChannelAdapter):
    """WeCom callback adapter (token + AES msg crypto)."""

    platform = PLATFORM_WECOM

    def __init__(self, *, token: str | None = None, encoding_aes_key: str | None = None) -> None:
        self.token = token or os.environ.get("WECOM_TOKEN", "")
        self.encoding_aes_key = encoding_aes_key or os.environ.get("WECOM_ENCODING_AES_KEY", "")

    # ---- verify ---------------------------------------------------------

    def verify(
        self, *, payload: Mapping[str, Any], headers: Mapping[str, str], query: Mapping[str, str]
    ) -> bool:
        """Verify the WeCom ``msg_signature`` for a JSON or XML callback."""
        timestamp = query.get("timestamp") or headers.get("x-wecom-timestamp") or ""
        nonce = query.get("nonce") or headers.get("x-wecom-nonce") or ""
        msg_signature = query.get("msg_signature") or headers.get("x-wecom-signature") or ""
        encrypt = payload.get("Encrypt") or payload.get("encrypt")
        if not encrypt and isinstance(payload.get("xml"), str):
            encrypt = self._extract_encrypt(payload["xml"])
        return verify_wecom_signature(
            token=self.token,
            encrypt=str(encrypt or ""),
            msg_signature=msg_signature,
            timestamp=timestamp,
            nonce=nonce,
        )

    def verify_url(self, *, signature: str, timestamp: str, nonce: str) -> bool:
        """Verify the WeCom server-URL verification (GET) signature."""
        return verify_wecom_url_signature(
            token=self.token,
            signature=signature,
            timestamp=timestamp,
            nonce=nonce,
        )

    @staticmethod
    def _extract_encrypt(xml_text: str) -> str:
        """Pull ``<Encrypt>`` out of a callback XML body."""
        try:
            return ET.fromstring(xml_text).findtext("Encrypt") or ""
        except ET.ParseError:
            return ""

    # ---- parse ----------------------------------------------------------

    def parse_inbound(
        self, *, payload: Mapping[str, Any], headers: Mapping[str, str]
    ) -> ChannelMessage | None:
        """Extract a text message from a decrypted JSON or XML callback."""
        raw_xml = payload.get("xml")
        if isinstance(raw_xml, str):
            return self._parse_xml(raw_xml)
        text = payload.get("Content") or payload.get("content")
        if not text:
            return None
        return ChannelMessage(
            platform=self.platform,
            external_chat_id=str(payload.get("ChatId") or payload.get("FromUserName") or ""),
            external_user_id=str(payload.get("FromUserName") or ""),
            text=str(text),
            raw_payload=dict(payload),
        )

    def _parse_xml(self, xml_text: str) -> ChannelMessage | None:
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError:
            return None
        if root.findtext("MsgType", "text") != "text":
            return None
        text = root.findtext("Content") or ""
        from_user = root.findtext("FromUserName") or ""
        if not text or not from_user:
            return None
        return ChannelMessage(
            platform=self.platform,
            external_chat_id=from_user,
            external_user_id=from_user,
            text=text,
            raw_payload={"FromUserName": from_user, "Content": text},
        )

    # ---- send -----------------------------------------------------------

    async def send_reply(self, client: Any, *, external_chat_id: str, text: str) -> dict[str, Any]:
        """Send a (chunked) reply via the obase WeCom client."""
        results: list[dict[str, Any]] = []
        for chunk in ChannelAdapter.chunk_for_platform(text, platform=self.platform):
            results.append(await client.send_message(external_chat_id, chunk))
        return {"ok": True, "chunks": len(results), "results": results}


__all__ = ["WeComAdapter"]
