"""veya.im.slack — Slack bot gateway adapter (Layer 4).

Listens for Slack Events API messages, pseudo-anonymizes the sender id
(SPEC §5.5.1), and schedules the text into the assembled ``agentic_loop``
engine in the background, replying with the thinking log (decision trail)
and the final result via ``chat.postMessage``.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from typing import Any

import httpx

from veya.im.pseudo import anonymize_user_id

_bg_tasks: set = set()

DEFAULT_WEBHOOK_ENDPOINT = "/im/slack/events"
DEFAULT_CHAT_POST = "https://slack.com/api/chat.postMessage"
MAX_CHUNK = 3900  # Slack message text limit is 40k chars; chunk at 3900


class SlackGateway:
    """Slack bot gateway: Events API → pseudo-anonymize → background agent run.

    The ``runner`` callable receives ``(text, user_ref)`` and returns an async
    iterator of step dicts (decision trail) — the default runner drives the
    assembled ``agentic_loop`` engine through ``veya.server.manifests``.
    """

    def __init__(
        self,
        *,
        bot_token: str | None = None,
        runner: Callable[..., Any] | None = None,
        reply_fn: Callable[..., Any] | None = None,
        webhook_path: str = DEFAULT_WEBHOOK_ENDPOINT,
    ) -> None:
        self.bot_token = bot_token
        self._runner = runner
        self._reply_fn = reply_fn
        self.webhook_path = webhook_path
        self._pending: dict[str, asyncio.Task] = {}

    # -- message listening -------------------------------------------------
    def parse_payload(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        """Extract ``{text, user_id, channel}`` from a Slack Events API payload."""
        if payload.get("type") == "url_verification":
            return {
                "challenge": payload.get("challenge"),
                "text": None,
                "user_id": None,
                "channel": None,
            }
        event = payload.get("event") or {}
        if event.get("type") != "message" or event.get("subtype"):
            return None
        text = (event.get("text") or "").strip()
        if not text:
            return None
        return {
            "text": text,
            "user_id": event.get("user") or event.get("bot_id") or "unknown",
            "channel": event.get("channel", ""),
            "challenge": None,
        }

    # -- pseudo-anonymization (SPEC §5.5.1) ---------------------------------
    @staticmethod
    def anonymize(user_id: str) -> str:
        return anonymize_user_id(user_id)

    # -- background scheduling ----------------------------------------------
    async def handle_event(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Process one Events API payload; ACK immediately.

        Returns the challenge for url_verification, or an ACK dict for messages.
        """
        parsed = self.parse_payload(payload)
        if parsed is None:
            return {"ok": True, "ignored": True}
        if parsed.get("challenge") is not None:
            return {"challenge": parsed["challenge"]}
        if not parsed.get("text"):
            return {"ok": True, "ignored": True}

        user_ref = self.anonymize(parsed["user_id"])

        if self._reply_fn is None:
            self._reply_fn = self._default_reply(parsed["channel"])

        task = _task_ref = asyncio.create_task(self._dispatch(parsed["text"], user_ref, parsed["channel"]))
        self._pending[user_ref] = task
        return {"ok": True, "user_ref": user_ref, "channel": parsed["channel"]}

    async def _dispatch(self, text: str, user_ref: str, channel: str) -> None:
        """Run the agent in the background, streaming segmented replies."""
        reply = self._reply_fn
        try:
            if self._runner is None:
                self._runner = _default_runner()
            async for step in self._runner(text, user_ref):
                event = step.get("event", "step")
                if event in ("tool_call", "tool_result", "llm_call", "thinking", "session_done"):
                    await self._chunked_reply(reply, channel, _step_text(step))
            if not any(s.get("event") == "session_done" for s in []):  # rely on runner events
                await self._chunked_reply(reply, channel, "[agent finished]")
        except Exception as exc:  # pragma: no cover - defensive
            await self._chunked_reply(reply, channel, f"[agent error] {exc}")
        finally:
            self._pending.pop(user_ref, None)

    async def _chunked_reply(self, reply: Callable[..., Any], channel: str, text: str) -> None:
        for i in range(0, len(text), MAX_CHUNK):
            chunk = text[i : i + MAX_CHUNK]
            out = reply(channel=channel, text=chunk)
            if hasattr(out, "__await__"):
                await out
            await asyncio.sleep(0)

    # -- bot API ------------------------------------------------------------
    def _default_reply(self, channel: str) -> Callable[..., Any]:
        async def reply(*, channel: str = channel, text: str = "") -> None:
            if not self.bot_token:
                return  # offline mode — no credentials
            async with httpx.AsyncClient(timeout=15.0) as client:
                await client.post(
                    DEFAULT_CHAT_POST,
                    headers={"Authorization": f"Bearer {self.bot_token}"},
                    json={"channel": channel, "text": text},
                )

        return reply


def _step_text(step: dict[str, Any]) -> str:
    event = step.get("event", "step")
    detail = (
        step.get("detail")
        or step.get("data")
        or step.get("tool")
        or step.get("result_preview")
        or step.get("content")
        or ""
    )
    if isinstance(detail, dict):
        detail = json.dumps(detail, ensure_ascii=False)[:160]
    return f"{event}: {detail}" if detail else event


def _default_runner():
    """Async generator driving the assembled agentic loop engine."""

    async def runner(text: str, user_ref: str):
        from server.coordinator_master import master_coordinator

        result = await master_coordinator.chat_stream(text, session_id=None, max_rounds=3)
        turn = result.get("final_answer") or result.get("error", "")
        yield {"event": "session_start", "user_ref": user_ref, "ts": time.time()}
        yield {"event": "result", "content": turn, "cost": result.get("cost_usd", 0.0)}
        yield {"event": "session_done", "user_ref": user_ref, "ts": time.time()}

    return runner


def make_slack_router(gateway: SlackGateway | None = None):
    """Build a FastAPI APIRouter exposing the Slack Events endpoint."""
    from fastapi import APIRouter

    gw = gateway or SlackGateway()
    router = APIRouter(prefix="/im/slack", tags=["im-slack"])

    @router.post("/events")
    async def events(payload: dict[str, Any]) -> dict[str, Any]:
        return await gw.handle_event(payload)

    return router


__all__ = ["DEFAULT_WEBHOOK_ENDPOINT", "SlackGateway", "make_slack_router"]
