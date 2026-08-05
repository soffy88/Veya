"""veya.im.feishu — Feishu (Lark) bot gateway adapter (Layer 4).

Listens for Feishu webhook messages, pseudo-anonymizes the sender id
(SPEC §5.5.1), and schedules the text into the assembled ``agentic_loop``
engine in the background, replying with the thinking log (decision trail)
and the final result via the bot API.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections.abc import Callable
from typing import Any

import httpx

from veya.im.pseudo import anonymize_user_id

_bg_tasks: set = set()

DEFAULT_WEBHOOK_ENDPOINT = "/im/feishu/webhook"
DEFAULT_BOT_API = "https://open.feishu.cn/open-apis/im/v1/messages"
MAX_CHUNK = 1900  # Feishu message text limit is generous; keep segments sane


class FeishuGateway:
    """Feishu bot gateway: webhook → pseudo-anonymize → background agent run.

    The ``runner`` callable receives ``(text, user_ref)`` and returns an async
    iterator of step dicts (decision trail) — the default runner drives the
    assembled ``agentic_loop`` engine through ``veya.server.manifests``.
    """

    def __init__(
        self,
        *,
        app_id: str | None = None,
        app_secret: str | None = None,
        runner: Callable[..., Any] | None = None,
        reply_fn: Callable[..., Any] | None = None,
        webhook_path: str = DEFAULT_WEBHOOK_ENDPOINT,
    ) -> None:
        self.app_id = app_id
        self.app_secret = app_secret
        self._runner = runner
        self._reply_fn = reply_fn
        self.webhook_path = webhook_path
        self._pending: dict[str, asyncio.Task] = {}

    # -- message listening -------------------------------------------------
    def parse_payload(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        """Extract ``{text, user_id, chat_id}`` from a Feishu webhook payload."""
        event = payload.get("event") or payload.get("data", {})
        message = event.get("message") or {}
        sender = event.get("sender") or {}
        text = message.get("content") or ""
        if isinstance(text, str):
            with contextlib.suppress(json.JSONDecodeError):
                text = json.loads(text).get("text", text)
        if not text:
            return None
        return {
            "text": text,
            "user_id": sender.get("sender_id", {}).get("open_id")
            or sender.get("open_id")
            or message.get("chat_id", "unknown"),
            "chat_id": message.get("chat_id") or event.get("chat_id", ""),
        }

    # -- pseudo-anonymization (SPEC §5.5.1) ---------------------------------
    @staticmethod
    def anonymize(user_id: str) -> str:
        return anonymize_user_id(user_id)

    # -- background scheduling ----------------------------------------------
    async def handle_event(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Process one webhook event: anonymize + schedule background run.

        Returns immediately with an ACK; the agent runs as a background task
        and replies through ``reply_fn`` (or the default bot API caller).
        """
        parsed = self.parse_payload(payload)
        if parsed is None:
            return {"ok": False, "reason": "no text"}
        user_ref = self.anonymize(parsed["user_id"])

        if self._reply_fn is None:
            self._reply_fn = self._default_reply(parsed["chat_id"])

        task = _task_ref = asyncio.create_task(self._dispatch(parsed["text"], user_ref, parsed["chat_id"]))
        self._pending[user_ref] = task
        return {"ok": True, "user_ref": user_ref, "chat_id": parsed["chat_id"]}

    async def _dispatch(self, text: str, user_ref: str, chat_id: str) -> None:
        """Run the agent in the background, streaming segmented replies."""
        steps: list[dict[str, Any]] = []
        reply = self._reply_fn
        try:
            if self._runner is None:
                self._runner = _default_runner()
            async for step in self._runner(text, user_ref):
                steps.append(step)
                event = step.get("event", "step")
                if event in ("tool_call", "tool_result", "llm_call", "thinking", "session_done"):
                    await self._chunked_reply(reply, chat_id, _step_text(step))
            if not any(s.get("event") == "session_done" for s in steps):
                await self._chunked_reply(reply, chat_id, "[agent finished]")
        except Exception as exc:  # pragma: no cover - defensive
            await self._chunked_reply(reply, chat_id, f"[agent error] {exc}")
        finally:
            self._pending.pop(user_ref, None)

    async def _chunked_reply(self, reply: Callable[..., Any], chat_id: str, text: str) -> None:
        for i in range(0, len(text), MAX_CHUNK):
            chunk = text[i : i + MAX_CHUNK]
            out = reply(chat_id=chat_id, text=chunk)
            if hasattr(out, "__await__"):
                await out
            await asyncio.sleep(0)  # yield so segments interleave with engine steps

    # -- bot API ------------------------------------------------------------
    def _default_reply(self, chat_id: str) -> Callable[..., Any]:
        async def reply(*, chat_id: str = chat_id, text: str = "") -> None:
            token = await self._tenant_token()
            if not token:
                return
            payload = {
                "receive_id_type": "chat_id",
                "receive_id": chat_id,
                "msg_type": "text",
                "content": json.dumps({"text": text}, ensure_ascii=False),
            }
            async with httpx.AsyncClient(timeout=15.0) as client:
                await client.post(
                    DEFAULT_BOT_API,
                    headers={"Authorization": f"Bearer {token}"},
                    json=payload,
                )

        return reply

    async def _tenant_token(self) -> str | None:
        if not self.app_id or not self.app_secret:
            return None  # no credentials configured — offline mode
        url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                url,
                json={"app_id": self.app_id, "app_secret": self.app_secret},
            )
            data = resp.json()
            return data.get("tenant_access_token")


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


def make_feishu_router(gateway: FeishuGateway | None = None):
    """Build a FastAPI APIRouter exposing the Feishu webhook endpoint."""
    from fastapi import APIRouter

    gw = gateway or FeishuGateway()
    router = APIRouter(prefix="/im/feishu", tags=["im-feishu"])

    @router.post("/webhook")
    async def webhook(payload: dict[str, Any]) -> dict[str, Any]:
        return await gw.handle_event(payload)

    return router


__all__ = ["DEFAULT_WEBHOOK_ENDPOINT", "FeishuGateway", "make_feishu_router"]
