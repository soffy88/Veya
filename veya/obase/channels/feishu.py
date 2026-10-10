"""veya/obase/channels/feishu.py — Feishu (Lark) Bot API client (oprim).

Atomic external HTTP calls. No business logic. Pure IO.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

import httpx

from veya.obase.channels import (
    PLATFORM_FEISHU,
    chunk_text,
)

_log = logging.getLogger("veya.obase.channels.feishu")

FEISHU_API_BASE = "https://open.feishu.cn/open-apis"


class FeishuClient:
    """Feishu Bot API client — thin wrapper around Feishu Bot API."""

    def __init__(
        self,
        app_id: str,
        app_secret: str,
        *,
        timeout: float = 30.0,
        max_retries: int = 3,
    ) -> None:
        if not app_id or not app_secret:
            raise ValueError("app_id and app_secret are required")
        self.app_id = app_id
        self.app_secret = app_secret
        self._timeout = timeout
        self._max_retries = max_retries
        self._client: httpx.AsyncClient | None = None
        self._tenant_token: str = ""
        self._token_expiry: float = 0.0

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout)
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _get_tenant_token(self) -> str:
        """Get or refresh tenant access token."""
        if self._tenant_token and time.time() < self._token_expiry:
            return self._tenant_token

        last_exc = None
        for attempt in range(self._max_retries):
            try:
                resp = await self.client.post(
                    f"{FEISHU_API_BASE}/auth/v3/tenant_access_token/internal",
                    json={"app_id": self.app_id, "app_secret": self.app_secret},
                )
                resp.raise_for_status()
                data = resp.json()
                if data.get("code") != 0:
                    raise RuntimeError(f"Feishu auth error: {data.get('msg')}")
                self._tenant_token = data["tenant_access_token"]
                expires_in = data.get("expire_in", 7200)
                self._token_expiry = time.time() + expires_in - 60
                return self._tenant_token
            except (httpx.RequestError, httpx.HTTPStatusError) as exc:
                last_exc = exc
                if attempt < self._max_retries - 1:
                    await asyncio.sleep(0.5 * (attempt + 1))
                    continue
                raise
        raise last_exc  # type: ignore[misc]

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Make an authenticated request with retry logic."""
        token = await self._get_tenant_token()
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

        last_exc = None
        for attempt in range(self._max_retries):
            try:
                resp = await self.client.request(
                    method,
                    f"{FEISHU_API_BASE}{path}",
                    headers=headers,
                    json=json_data,
                )
                resp.raise_for_status()
                data = resp.json()
                if data.get("code") != 0:
                    # Token might be expired, force refresh once
                    if data.get("code") in (99991663, 99991664) and attempt == 0:
                        self._token_expiry = 0
                        continue
                    raise RuntimeError(f"Feishu API error: {data.get('msg')}")
                return data.get("data", {})
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
        chat_id: str,
        text: str,
        *,
        msg_type: str = "text",
        receive_id_type: str = "chat_id",
    ) -> dict[str, Any]:
        """Send a message to a chat."""
        if msg_type == "text":
            content = json.dumps({"text": text}, ensure_ascii=False)
        elif msg_type == "post":
            content = json.dumps(text, ensure_ascii=False)  # text is already a post dict
        else:
            raise ValueError(f"Unsupported msg_type: {msg_type}")

        payload = {
            "receive_id_type": receive_id_type,
            "receive_id": chat_id,
            "msg_type": msg_type,
            "content": content,
        }
        return await self._request("POST", "/im/v1/messages", json_data=payload)

    async def send_chunked(
        self,
        chat_id: str,
        text: str,
        *,
        msg_type: str = "text",
    ) -> list[dict[str, Any]]:
        """Send a long message as multiple chunks."""
        chunks = chunk_text(text, platform=PLATFORM_FEISHU)
        results = []
        for chunk in chunks:
            results.append(await self.send_message(chat_id, chunk, msg_type=msg_type))
            await asyncio.sleep(0.05)
        return results

    async def send_post_card(
        self,
        chat_id: str,
        title: str,
        content: list[list[dict[str, Any]]],
        *,
        receive_id_type: str = "chat_id",
    ) -> dict[str, Any]:
        """Send a rich post card message."""
        post_content = {
            "post": {
                "zh_cn": {
                    "title": title,
                    "content": content,
                }
            }
        }
        return await self.send_message(
            chat_id, post_content, msg_type="post", receive_id_type=receive_id_type
        )

    # ---- Webhook management (oprim: single HTTP call) ----

    async def create_webhook(
        self,
        url: str,
        *,
        event_types: list[str] | None = None,
    ) -> dict[str, Any]:
        """Create an event subscription webhook (requires app registration)."""
        payload = {"url": url}
        if event_types:
            payload["event_types"] = event_types
        return await self._request("POST", "/event/v1/subscriptions", json_data=payload)

    # ---- Bot info (oprim: single HTTP call) ----

    async def get_bot_info(self) -> dict[str, Any]:
        """Get bot info."""
        return await self._request("GET", "/bot/v3/info", json_data=None)


# ---- Convenience factory ----


async def create_feishu_client(
    app_id: str,
    app_secret: str,
    *,
    timeout: float = 30.0,
) -> FeishuClient:
    """Create and verify a Feishu client."""
    client = FeishuClient(app_id, app_secret, timeout=timeout)
    await client._get_tenant_token()
    return client


__all__ = ["FEISHU_API_BASE", "FeishuClient", "create_feishu_client"]
