"""veya/obase/channels/slack.py — Slack Web API client (oprim).

Atomic external HTTP calls. No business logic. Pure IO.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

_log = logging.getLogger("veya.obase.channels.slack")

SLACK_API_BASE = "https://slack.com/api"


class SlackClient:
    """Slack Web API client — thin wrapper around chat.postMessage."""

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

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.bot_token}",
            "Content-Type": "application/json; charset=utf-8",
        }

    async def post_message(
        self,
        channel: str,
        text: str,
    ) -> dict[str, Any]:
        """Post one text message. Returns the Slack response object."""

        last_exc: Exception | None = None
        for attempt in range(self._max_retries):
            try:
                resp = await self.client.post(
                    f"{SLACK_API_BASE}/chat.postMessage",
                    headers=self._headers(),
                    json={"channel": channel, "text": text},
                )
                resp.raise_for_status()
                data: dict[str, Any] = resp.json()
                if not data.get("ok"):
                    raise RuntimeError(f"Slack API error: {data.get('error')}")
                return data
            except (httpx.RequestError, httpx.HTTPStatusError) as exc:
                last_exc = exc
                if attempt < self._max_retries - 1:
                    await asyncio.sleep(0.5 * (attempt + 1))
                    continue
                raise
        raise last_exc  # type: ignore[misc]


async def create_slack_client(bot_token: str) -> SlackClient:
    """Build a Slack client. No network traffic happens here."""

    return SlackClient(bot_token)


__all__ = ["SlackClient", "create_slack_client"]
