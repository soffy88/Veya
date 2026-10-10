"""veya/obase/channels/telegram.py — Telegram Bot API client (oprim).

Atomic external HTTP calls. No business logic. Pure IO.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from veya.obase.channels import (
    PLATFORM_TELEGRAM,
    chunk_text,
)

_log = logging.getLogger("veya.obase.channels.telegram")

TELEGRAM_API_BASE = "https://api.telegram.org"


class TelegramClient:
    """Telegram Bot API client — thin wrapper around Bot API."""

    def __init__(
        self,
        bot_token: str,
        *,
        timeout: float = 30.0,
        max_retries: int = 3,
    ) -> None:
        if not bot_token:
            raise ValueError("bot_token is required")
        self.bot_token = bot_token
        self._timeout = timeout
        self._max_retries = max_retries
        self._client: httpx.AsyncClient | None = None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout)
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _url(self, method: str) -> str:
        return f"{TELEGRAM_API_BASE}/bot{self.bot_token}/{method}"

    async def _request(
        self,
        method: str,
        *,
        json_data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Make a request with retry logic."""
        last_exc = None
        for attempt in range(self._max_retries):
            try:
                resp = await self.client.post(self._url(method), json=json_data)
                resp.raise_for_status()
                data = resp.json()
                if not data.get("ok"):
                    raise RuntimeError(f"Telegram API error: {data.get('description')}")
                return data.get("result", {})
            except (httpx.RequestError, httpx.HTTPStatusError) as exc:
                last_exc = exc
                if attempt < self._max_retries - 1:
                    await asyncio.sleep(0.5 * (attempt + 1))
                    continue
                raise
        raise last_exc  # type: ignore[misc]

    # ---- Core send (oprim: single HTTP call) ----

    async def send_message(
        self,
        chat_id: int | str,
        text: str,
        *,
        parse_mode: str | None = "Markdown",
        disable_web_page_preview: bool = True,
        reply_to_message_id: int | None = None,
    ) -> dict[str, Any]:
        """Send a text message. Returns the sent message object."""
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": disable_web_page_preview,
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode
        if reply_to_message_id:
            payload["reply_to_message_id"] = reply_to_message_id
        return await self._request("sendMessage", json_data=payload)

    async def send_chunked(
        self,
        chat_id: int | str,
        text: str,
        *,
        parse_mode: str | None = "Markdown",
    ) -> list[dict[str, Any]]:
        """Send a long message as multiple chunks. Returns list of sent messages."""
        chunks = chunk_text(text, platform=PLATFORM_TELEGRAM)
        results = []
        for chunk in chunks:
            results.append(await self.send_message(chat_id, chunk, parse_mode=parse_mode))
            # Small delay to avoid rate limits
            await asyncio.sleep(0.05)
        return results

    # ---- Typing indicator (oprim: single HTTP call) ----

    async def send_chat_action(
        self,
        chat_id: int | str,
        action: str = "typing",
    ) -> dict[str, Any]:
        """Send a chat action (typing, upload_photo, etc.)."""
        return await self._request(
            "sendChatAction",
            json_data={"chat_id": chat_id, "action": action},
        )

    # ---- Webhook management (oprim: single HTTP call) ----

    async def set_webhook(
        self,
        url: str,
        *,
        secret_token: str | None = None,
        allowed_updates: list[str] | None = None,
    ) -> dict[str, Any]:
        """Set webhook URL."""
        payload: dict[str, Any] = {"url": url}
        if secret_token:
            payload["secret_token"] = secret_token
        if allowed_updates:
            payload["allowed_updates"] = allowed_updates
        return await self._request("setWebhook", json_data=payload)

    async def delete_webhook(self, *, drop_pending_updates: bool = True) -> dict[str, Any]:
        """Delete webhook."""
        return await self._request(
            "deleteWebhook",
            json_data={"drop_pending_updates": drop_pending_updates},
        )

    async def get_webhook_info(self) -> dict[str, Any]:
        """Get current webhook info."""
        return await self._request("getWebhookInfo", json_data={})

    # ---- Polling (oprim: single HTTP call) ----

    async def get_updates(
        self,
        *,
        offset: int | None = None,
        limit: int = 100,
        timeout: int = 30,
        allowed_updates: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Get updates via long polling."""
        params: dict[str, Any] = {"limit": limit, "timeout": timeout}
        if offset is not None:
            params["offset"] = offset
        if allowed_updates:
            params["allowed_updates"] = allowed_updates
        resp = await self.client.get(self._url("getUpdates"), params=params)
        resp.raise_for_status()
        data = resp.json()
        if not data.get("ok"):
            raise RuntimeError(f"Telegram API error: {data.get('description')}")
        return data.get("result", [])

    # ---- Bot info (oprim: single HTTP call) ----

    async def get_me(self) -> dict[str, Any]:
        """Get bot info."""
        return await self._request("getMe", json_data={})


# ---- Convenience factory (not an oprim, just a helper) ----


async def create_telegram_client(
    bot_token: str,
    *,
    timeout: float = 30.0,
) -> TelegramClient:
    """Create and verify a Telegram client."""
    client = TelegramClient(bot_token, timeout=timeout)
    # Verify token by calling getMe
    await client.get_me()
    return client


__all__ = ["TELEGRAM_API_BASE", "TelegramClient", "create_telegram_client"]
