"""veya/obase/channels/dingtalk.py — DingTalk Bot API client (oprim).

Atomic external HTTP calls. No business logic. Pure IO.
Supports both Enterprise Bot (OAuth) and Group Chat Bot (webhook).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import logging
import time
from typing import Any
from urllib.parse import quote_plus

import httpx

from veya.obase.channels import (
    PLATFORM_DINGTALK,
    chunk_text,
    verify_dingtalk_signature,
)

_log = logging.getLogger("veya.obase.channels.dingtalk")

DINGTALK_OAUTH_URL = "https://api.dingtalk.com/v1.0/oauth2/accessToken"
DINGTALK_SEND_URL = "https://api.dingtalk.com/v1.0/robot/oToMessages/batchSend"
DINGTALK_GROUP_SEND_URL = "https://oapi.dingtalk.com/robot/send"


class DingTalkClient:
    """DingTalk Bot API client — supports Enterprise Bot and Group Chat Bot."""

    def __init__(
        self,
        *,
        app_key: str | None = None,
        app_secret: str | None = None,
        webhook_url: str | None = None,
        webhook_secret: str | None = None,
        timeout: float = 30.0,
        max_retries: int = 3,
    ) -> None:
        self.app_key = app_key
        self.app_secret = app_secret
        self.webhook_url = webhook_url
        self.webhook_secret = webhook_secret
        self._timeout = timeout
        self._max_retries = max_retries
        self._client: httpx.AsyncClient | None = None
        self._access_token: str = ""
        self._token_expiry: float = 0.0

        # Validate configuration
        has_enterprise = bool(app_key and app_secret)
        has_group = bool(webhook_url)
        if not has_enterprise and not has_group:
            raise ValueError("Either (app_key, app_secret) or webhook_url is required")

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout)
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def is_enterprise_bot(self) -> bool:
        return bool(self.app_key and self.app_secret)

    def is_group_bot(self) -> bool:
        return bool(self.webhook_url)

    # ---- Enterprise Bot: Token management ----

    async def _get_access_token(self) -> str:
        """Get or refresh DingTalk OAuth access token (enterprise bot)."""
        if not self.is_enterprise_bot():
            raise RuntimeError("Not configured as enterprise bot")

        if self._access_token and time.time() < self._token_expiry:
            return self._access_token

        last_exc = None
        for attempt in range(self._max_retries):
            try:
                resp = await self.client.post(
                    DINGTALK_OAUTH_URL,
                    json={"appKey": self.app_key, "appSecret": self.app_secret},
                    headers={"Content-Type": "application/json"},
                )
                resp.raise_for_status()
                data = resp.json()
                self._access_token = data.get("accessToken", "")
                expires_in = data.get("expireIn", 7200)
                self._token_expiry = time.time() + expires_in - 60
                return self._access_token
            except (httpx.RequestError, httpx.HTTPStatusError) as exc:
                last_exc = exc
                if attempt < self._max_retries - 1:
                    await asyncio.sleep(0.5 * (attempt + 1))
                    continue
                raise
        raise last_exc  # type: ignore[misc]

    # ---- Signature verification (oprim: single crypto op) ----

    @staticmethod
    def verify_signature(
        timestamp: str,
        sign: str,
        webhook_secret: str,
    ) -> bool:
        """Verify DingTalk group chat bot webhook signature (delegates to obase oprim)."""
        return verify_dingtalk_signature(
            webhook_secret=webhook_secret,
            timestamp=timestamp,
            sign=sign,
        )

    def _sign_group_request(self, timestamp: str | None = None) -> tuple[str, str]:
        """Generate timestamp and signature for group bot requests."""
        ts = timestamp or str(int(time.time() * 1000))
        sign = ""
        if self.webhook_secret:
            string_to_sign = f"{ts}\n{self.webhook_secret}"
            sig_bytes = hmac.new(
                self.webhook_secret.encode("utf-8"),
                string_to_sign.encode("utf-8"),
                hashlib.sha256,
            ).digest()
            sign = quote_plus(base64.b64encode(sig_bytes).decode("utf-8"))
        return ts, sign

    # ---- Core send (oprim: single HTTP call) ----

    async def send_message(
        self,
        text: str,
        *,
        session_webhook: str | None = None,
        msgtype: str = "text",
    ) -> dict[str, Any]:
        """Send a message. Use session_webhook for enterprise bot replies."""
        if self.is_enterprise_bot() and session_webhook:
            return await self._send_enterprise_reply(session_webhook, text, msgtype)
        elif self.is_group_bot():
            return await self._send_group_message(text, msgtype)
        else:
            raise RuntimeError("No valid send path configured")

    async def _send_enterprise_reply(
        self,
        session_webhook: str,
        text: str,
        msgtype: str,
    ) -> dict[str, Any]:
        """Send reply via enterprise bot session webhook."""
        token = await self._get_access_token()
        payload = {
            "msgtype": msgtype,
            msgtype: {"content": text},
        }
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

        last_exc = None
        for attempt in range(self._max_retries):
            try:
                resp = await self.client.post(
                    session_webhook,
                    json=payload,
                    headers=headers,
                )
                resp.raise_for_status()
                return resp.json()
            except (httpx.RequestError, httpx.HTTPStatusError) as exc:
                last_exc = exc
                if attempt < self._max_retries - 1:
                    await asyncio.sleep(0.5 * (attempt + 1))
                    continue
                raise
        raise last_exc  # type: ignore[misc]

    async def _send_group_message(
        self,
        text: str,
        msgtype: str,
    ) -> dict[str, Any]:
        """Send message via group chat bot webhook."""
        if not self.webhook_url:
            raise RuntimeError("Group bot webhook_url not configured")

        timestamp, sign = self._sign_group_request()
        full_url = f"{self.webhook_url}&timestamp={timestamp}&sign={sign}"

        payload = {
            "msgtype": msgtype,
            msgtype: {"content": text[:20000]},  # DingTalk group limit
        }

        last_exc = None
        for attempt in range(self._max_retries):
            try:
                resp = await self.client.post(
                    full_url,
                    json=payload,
                    headers={"Content-Type": "application/json"},
                )
                resp.raise_for_status()
                return resp.json()
            except (httpx.RequestError, httpx.HTTPStatusError) as exc:
                last_exc = exc
                if attempt < self._max_retries - 1:
                    await asyncio.sleep(0.5 * (attempt + 1))
                    continue
                raise
        raise last_exc  # type: ignore[misc]

    async def send_chunked(
        self,
        text: str,
        *,
        session_webhook: str | None = None,
        msgtype: str = "text",
    ) -> list[dict[str, Any]]:
        """Send a long message as multiple chunks."""
        chunks = chunk_text(text, platform=PLATFORM_DINGTALK)
        results = []
        for chunk in chunks:
            results.append(
                await self.send_message(chunk, session_webhook=session_webhook, msgtype=msgtype)
            )
            await asyncio.sleep(0.05)
        return results

    # ---- Bot info (oprim: single HTTP call) ----

    async def get_bot_info(self) -> dict[str, Any]:
        """Get bot info (enterprise bot only)."""
        if not self.is_enterprise_bot():
            raise RuntimeError("Not configured as enterprise bot")
        token = await self._get_access_token()
        # This endpoint varies; using a common one
        resp = await self.client.get(
            "https://api.dingtalk.com/v1.0/robot/info",
            headers={"Authorization": f"Bearer {token}"},
        )
        resp.raise_for_status()
        return resp.json()


# ---- Convenience factory ----


async def create_dingtalk_client(
    *,
    app_key: str | None = None,
    app_secret: str | None = None,
    webhook_url: str | None = None,
    webhook_secret: str | None = None,
    timeout: float = 30.0,
) -> DingTalkClient:
    """Create and verify a DingTalk client."""
    client = DingTalkClient(
        app_key=app_key,
        app_secret=app_secret,
        webhook_url=webhook_url,
        webhook_secret=webhook_secret,
        timeout=timeout,
    )
    if client.is_enterprise_bot():
        await client._get_access_token()
    return client


__all__ = [
    "DINGTALK_GROUP_SEND_URL",
    "DINGTALK_OAUTH_URL",
    "DINGTALK_SEND_URL",
    "DingTalkClient",
    "create_dingtalk_client",
]
