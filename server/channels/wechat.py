"""server/channels/wechat.py — personal-WeChat (iLink) adapter (Layer 4).

Polling channel, NOT a webhook channel: there is no signature to verify
(``verify`` always fails closed; the polling path never calls it). The
edge poller script owns the iLink session and forwards inbound items to
``POST /api/v1/channels/wechat/inbound`` (edge-secret authenticated); the
backend sends replies back through :class:`WechatPollerClient`, which POSTs
to the poller's loopback reply endpoint. Same split as the window bridge:
the edge owns the account session, the hub owns the brain.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any

import httpx

from veya.obase.channels import ChannelMessage

from .base import ChannelAdapter


class WechatAdapter(ChannelAdapter):
    """iLink message adapter: normalize one message item, send via poller."""

    platform = "wechat"

    def verify(
        self,
        *,
        payload: Mapping[str, Any],
        headers: Mapping[str, str],
        query: Mapping[str, str],
    ) -> bool:
        """No webhook exists for polling channels: always refuse."""

        return False

    def parse_inbound(
        self,
        *,
        payload: Mapping[str, Any],
        headers: Mapping[str, str],
    ) -> ChannelMessage | None:
        """Normalize one iLink message item into a ChannelMessage."""

        if payload.get("type") != "text":
            return None
        text = payload.get("text")
        conversation = payload.get("conversation")
        sender = payload.get("from")
        if not text or not conversation or not sender:
            return None
        return ChannelMessage(
            platform=self.platform,
            external_chat_id=str(conversation),
            external_user_id=str(sender),
            text=str(text),
            raw_payload=dict(payload),
        )

    async def send_reply(self, client: Any, *, external_chat_id: str, text: str) -> dict[str, Any]:
        """Send a (chunked) reply through the poller client."""

        results: list[dict[str, Any]] = []
        for chunk in chunk_text_for_platform(text):
            results.append(await client.send_text(external_chat_id, chunk))
        return {"ok": True, "chunks": len(results), "results": results}


def chunk_text_for_platform(text: str) -> list[str]:
    """Long replies are split to the default chunk limit."""

    return ChannelAdapter.chunk_for_platform(text, platform="wechat")


class WechatPollerClient:
    """Backend-side client: POST replies to the edge poller's reply endpoint.

    The poller listens on the host; the backend container reaches it through
    the docker host address (same pattern as the window bridge). The edge
    secret authenticates both directions and lives in the environment only.
    """

    def __init__(
        self,
        *,
        poller_url: str | None = None,
        edge_secret: str | None = None,
    ) -> None:
        self.poller_url = (
            poller_url or os.environ.get("WECHAT_POLLER_URL") or "http://192.168.80.1:19451/reply"
        ).rstrip("/")
        self.edge_secret = edge_secret or os.environ.get("WECHAT_EDGE_SECRET", "")
        if not self.edge_secret:
            raise ValueError("WECHAT_EDGE_SECRET is not configured")
        self._client: httpx.AsyncClient | None = None

    @property
    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=30.0)
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def send_text(self, chat_id: str, text: str) -> dict[str, Any]:
        """Deliver one reply chunk to the poller for iLink sending."""

        resp = await self._http.post(
            self.poller_url,
            headers={"Authorization": f"Bearer {self.edge_secret}"},
            json={"chat_id": chat_id, "text": text},
        )
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, dict):
            raise RuntimeError("poller returned a non-object")
        return data


__all__ = ["WechatAdapter", "WechatPollerClient", "chunk_text_for_platform"]
