"""veya/oskill/im/feishu.py — Feishu (Lark) Bot Gateway (Layer 4).

Uses obase.channels.FeishuClient for all IO (3O compliance).
Pure formatting delegated to this module's format_* functions.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Callable
from typing import Any

try:
    from fastapi import APIRouter, HTTPException, Request

    _HAS_FASTAPI = True
except ImportError:
    _HAS_FASTAPI = False
    APIRouter = object  # type: ignore
    HTTPException = Exception  # type: ignore
    Request = object  # type: ignore

from veya.obase.channels.feishu import FeishuClient, create_feishu_client
from veya.obase.channels import verify_feishu_signature
from veya.oskill.im.pseudo import anonymize_user_id

logger = logging.getLogger("veya.oskill.im.feishu")

_bg_tasks: set = set()

DEFAULT_WEBHOOK_ENDPOINT = "/im/feishu/webhook"


class FeishuGateway:
    """Feishu bot gateway: webhook → pseudo-anonymize → background agent run.

    IO delegated to veya.obase.channels.FeishuClient (oprim).
    Formatting uses this module's pure formatters (oskill).
    """

    def __init__(
        self,
        *,
        app_id: str | None = None,
        app_secret: str | None = None,
        runner: Callable[..., Any] | None = None,
        webhook_path: str = DEFAULT_WEBHOOK_ENDPOINT,
    ) -> None:
        self.app_id = app_id or os.environ.get("FEISHU_APP_ID", "")
        self.app_secret = app_secret or os.environ.get("FEISHU_APP_SECRET", "")
        self._runner = runner
        self.webhook_path = webhook_path
        self._client: FeishuClient | None = None
        self._pending: dict[str, asyncio.Task] = {}

    async def _get_client(self) -> FeishuClient:
        if self._client is None:
            if not self.app_id or not self.app_secret:
                raise RuntimeError("FEISHU_APP_ID and FEISHU_APP_SECRET not configured")
            self._client = await create_feishu_client(self.app_id, self.app_secret)
        return self._client

    # -- message listening -------------------------------------------------

    def parse_payload(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        """Extract {text, user_id, chat_id} from a Feishu webhook payload."""
        event = payload.get("event") or payload.get("data", {})
        message = event.get("message") or {}
        sender = event.get("sender") or {}
        text = message.get("content") or ""
        if isinstance(text, str):
            with __import__("contextlib").suppress(json.JSONDecodeError):
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
        and replies through the FeishuClient.
        """
        parsed = self.parse_payload(payload)
        if parsed is None:
            return {"ok": False, "reason": "no text"}

        user_ref = self.anonymize(parsed["user_id"])

        task = asyncio.create_task(
            self._dispatch(parsed["text"], user_ref, parsed["chat_id"])
        )
        self._pending[user_ref] = task
        return {"ok": True, "user_ref": user_ref, "chat_id": parsed["chat_id"]}

    async def _dispatch(self, text: str, user_ref: str, chat_id: str) -> None:
        """Run the agent in the background, streaming segmented replies."""
        client = await self._get_client()
        try:
            if self._runner is None:
                self._runner = _default_runner()
            async for step in self._runner(text, user_ref):
                event = step.get("event", "step")
                if event in ("tool_call", "tool_result", "llm_call", "thinking", "session_done"):
                    from .feishu import format_feishu_message
                    chunks = format_feishu_message(_step_text(step))
                    for chunk in chunks:
                        await client.send_message(chat_id, chunk, msg_type="post")
            if not any(s.get("event") == "session_done" for s in []):
                from .feishu import format_feishu_message
                chunks = format_feishu_message("[agent finished]")
                for chunk in chunks:
                    await client.send_message(chat_id, chunk, msg_type="post")
        except Exception as exc:  # pragma: no cover - defensive
            from .feishu import format_error
            chunks = format_error(user_ref, str(exc))
            for chunk in chunks:
                await client.send_message(chat_id, chunk, msg_type="post")
        finally:
            self._pending.pop(user_ref, None)


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
        import json
        detail = json.dumps(detail, ensure_ascii=False)[:160]
    return f"{event}: {detail}" if detail else event


def _default_runner():
    """Async generator driving the assembled agentic loop engine."""

    async def runner(text: str, user_ref: str):
        from server.coordinator_master import master_coordinator

        result = await master_coordinator.chat_stream(text, session_id=None)
        turn = result.get("final_answer") or result.get("error", "")
        import time
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
        # Verify signature if headers present
        timestamp = payload.get("header", {}).get("X-Lark-Request-Timestamp", "")
        nonce = payload.get("header", {}).get("X-Lark-Request-Nonce", "")
        signature = payload.get("header", {}).get("X-Lark-Signature", "")
        if timestamp and nonce and signature:
            if not verify_feishu_signature(
                app_id=gw.app_id,
                app_secret=gw.app_secret,
                payload=payload,
                timestamp=timestamp,
                nonce=nonce,
                signature=signature,
            ):
                raise HTTPException(status_code=401, detail="Invalid signature")
        return await gw.handle_event(payload)

    return router


# Re-export pure formatters for single-source guard
from .feishu import (  # noqa: F401,E402
    escape_feishu_text,
    format_feishu_post_content,
    build_text_block,
    build_at_block,
    build_link_block,
    format_feishu_message,
    format_task_started,
    format_task_result,
    format_error,
    format_status,
    format_help,
)

__all__ = [
    "DEFAULT_WEBHOOK_ENDPOINT",
    "FeishuGateway",
    "make_feishu_router",
    # Re-exported pure formatters
    "escape_feishu_text",
    "format_feishu_post_content",
    "build_text_block",
    "build_at_block",
    "build_link_block",
    "format_feishu_message",
    "format_task_started",
    "format_task_result",
    "format_error",
    "format_status",
    "format_help",
]