"""server/channels/slack.py — Slack adapter (Layer 4)."""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any

from veya.obase.channels import (
    PLATFORM_SLACK,
    ChannelMessage,
    verify_slack_signature,
)

from .base import ChannelAdapter


class SlackAdapter(ChannelAdapter):
    """Slack Events API adapter (webhook mode).

    Secrets are read from the environment only; nothing is stored in the repo.
    Signing-secret verification needs the RAW request body: the route passes
    it through ``raw_body``. Without it verification fails closed.
    """

    platform = PLATFORM_SLACK

    def __init__(
        self,
        *,
        bot_token: str | None = None,
        signing_secret: str | None = None,
    ) -> None:
        self.bot_token = bot_token if bot_token is not None else os.environ.get("SLACK_BOT_TOKEN", "")
        self.signing_secret = (
            signing_secret
            if signing_secret is not None
            else os.environ.get("SLACK_SIGNING_SECRET", "")
        )

    # ---- verify ---------------------------------------------------------

    def verify(
        self,
        *,
        payload: Mapping[str, Any],
        headers: Mapping[str, str],
        query: Mapping[str, str],
        raw_body: bytes | None = None,
    ) -> bool:
        """Check the ``X-Slack-Signature`` header against the raw body."""

        if raw_body is None:
            return False
        return verify_slack_signature(
            signing_secret=self.signing_secret or None,
            timestamp=headers.get("x-slack-request-timestamp"),
            raw_body=raw_body,
            provided=headers.get("x-slack-signature"),
        )

    # ---- challenge ------------------------------------------------------

    def challenge_response(self, payload: Mapping[str, Any]) -> dict[str, Any] | None:
        """Return the ``url_verification`` echo body, or None."""

        if payload.get("type") == "url_verification" and "challenge" in payload:
            return {"challenge": payload.get("challenge", "")}
        return None

    # ---- parse ----------------------------------------------------------

    def parse_inbound(
        self,
        *,
        payload: Mapping[str, Any],
        headers: Mapping[str, str],
    ) -> ChannelMessage | None:
        """Extract a user text message from a Slack Events API payload."""

        if payload.get("type") != "event_callback":
            return None
        event = payload.get("event")
        if not isinstance(event, dict):
            return None
        if event.get("type") != "message":
            return None
        if event.get("subtype") is not None or event.get("bot_id") is not None:
            return None
        text = event.get("text")
        channel = event.get("channel")
        user = event.get("user")
        if not text or not channel or not user:
            return None
        return ChannelMessage(
            platform=self.platform,
            external_chat_id=str(channel),
            external_user_id=str(user),
            text=str(text),
            raw_payload=dict(event),
        )

    # ---- send -----------------------------------------------------------

    async def send_reply(self, client: Any, *, external_chat_id: str, text: str) -> dict[str, Any]:
        """Send a (chunked) reply via the obase Slack client."""

        results: list[dict[str, Any]] = []
        for chunk in chunk_text_for_platform(text):
            results.append(await client.post_message(external_chat_id, chunk))
        return {"ok": True, "chunks": len(results), "results": results}


def chunk_text_for_platform(text: str) -> list[str]:
    """Long replies are split to Slack's chunk limit."""

    return ChannelAdapter.chunk_for_platform(text, platform=PLATFORM_SLACK)


__all__ = ["SlackAdapter"]
