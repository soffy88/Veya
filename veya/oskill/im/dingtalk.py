"""veya/oskill/im/dingtalk.py — DingTalk Bot Gateway (Layer 4).

Uses obase.channels.DingTalkClient for all IO (3O compliance).
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

from veya.obase.channels.dingtalk import DingTalkClient, create_dingtalk_client
from veya.obase.channels import PLATFORM_DINGTALK
from veya.oskill.im.pseudo import anonymize_user_id

logger = logging.getLogger("veya.oskill.im.dingtalk")

_bg_tasks: set = set()


class DingTalkGateway:
    """DingTalk robot gateway.

    IO delegated to veya.obase.channels.DingTalkClient (oprim).
    Formatting uses this module's pure formatters (oskill).
    """

    def __init__(
        self,
        *,
        app_key: str | None = None,
        app_secret: str | None = None,
        webhook_url: str | None = None,
        webhook_secret: str | None = None,
        runner: Callable[..., Any] | None = None,
    ):
        self.app_key = app_key or os.environ.get("DINGTALK_APP_KEY", "")
        self.app_secret = app_secret or os.environ.get("DINGTALK_APP_SECRET", "")
        self.webhook_url = webhook_url or os.environ.get("DINGTALK_WEBHOOK_URL", "")
        self.webhook_secret = webhook_secret or os.environ.get("DINGTALK_WEBHOOK_SECRET", "")
        self._runner = runner
        self._client: DingTalkClient | None = None

    async def _get_client(self) -> DingTalkClient:
        if self._client is None:
            self._client = await create_dingtalk_client(
                app_key=self.app_key or None,
                app_secret=self.app_secret or None,
                webhook_url=self.webhook_url or None,
                webhook_secret=self.webhook_secret or None,
            )
        return self._client

    async def handle_webhook(self, payload: dict, request: Request | None = None) -> dict:
        """Handle a DingTalk webhook message.

        Returns response dict to send back.
        """
        # Verify group chat bot signature if headers present
        if request:
            timestamp = request.headers.get("timestamp", "")
            sign = request.headers.get("sign", "")
            if timestamp and sign and self.webhook_secret:
                from veya.obase.channels import verify_dingtalk_signature
                if not verify_dingtalk_signature(
                    webhook_secret=self.webhook_secret,
                    timestamp=timestamp,
                    sign=sign,
                ):
                    raise HTTPException(status_code=401, detail="Invalid signature")

        # Extract message
        text_content = ""
        sender_id = "unknown"
        session_webhook = ""

        # Group chat bot format
        if "text" in payload:
            text_content = payload.get("text", {}).get("content", "")
            sender_id = payload.get("senderId", "unknown")
            session_webhook = payload.get("sessionWebhook", "")
        # Enterprise bot format
        elif "msgContent" in payload:
            text_content = payload.get("msgContent", "")
            sender_id = payload.get("senderStaffId", "unknown")
            session_webhook = payload.get("sessionWebhook", "")

        user_id = f"dingtalk:{sender_id}"
        pseudo_id = anonymize_user_id(user_id)

        if self._runner and text_content:
            # Background: run agent and reply
            _task_ref = asyncio.create_task(
                self._run_and_reply(
                    text_content,
                    session_webhook,
                    pseudo_id,
                    sender_id,
                )
            )
            _bg_tasks.add(_task_ref)

        from .dingtalk import format_task_started
        return format_task_started(sender_id, text_content[:50])

    async def _run_and_reply(
        self,
        prompt: str,
        session_webhook: str,
        pseudo_id: str,
        sender_id: str,
    ):
        """Run agent and send reply."""
        client = await self._get_client()
        try:
            result = await self._runner(prompt, user_ref=pseudo_id)
            status = result.get("status", "completed")
            content = result.get("result", "")
            if isinstance(content, list):
                text_parts = [b.get("text", "") for b in content if b.get("type") == "text"]
                content = "\n".join(text_parts) if text_parts else str(content)
            elif isinstance(content, dict):
                content = content.get("content", str(content))

            from .dingtalk import format_task_result, format_error
            if status == "completed":
                chunks = format_task_result(sender_id, prompt, str(content), result.get("cost_usd", 0.0))
            else:
                chunks = format_error(sender_id, str(content))

            for chunk in chunks:
                await client.send_message(
                    str(content)[:5000],
                    session_webhook=session_webhook,
                )
        except Exception as e:
            logger.error(f"DingTalk agent run failed: {e}")
            from .dingtalk import format_error
            chunk = format_error(sender_id, str(e))
            await client.send_message(
                chunk,
                session_webhook=session_webhook,
            )


def make_dingtalk_router(
    app_key: str | None = None,
    app_secret: str | None = None,
    webhook_url: str | None = None,
    webhook_secret: str | None = None,
) -> APIRouter:
    """Build a FastAPI router for DingTalk webhook.

    Mount:  app.include_router(make_dingtalk_router(), prefix="/im/dingtalk")
    """
    from server.coordinator_master import master_coordinator

    async def _default_runner(prompt: str, user_ref: str = "anon") -> dict:
        try:
            result = await master_coordinator.chat_stream(prompt, session_id=None)
            return {
                "status": result.get("status", "failed"),
                "content": result.get("final_answer") or result.get("error", ""),
                "cost_usd": result.get("cost_usd", 0.0),
                "user_ref": user_ref,
            }
        except Exception as exc:
            return {"status": "failed", "content": f"IM runner error: {exc}", "user_ref": user_ref}

    gateway = DingTalkGateway(
        app_key=app_key,
        app_secret=app_secret,
        webhook_url=webhook_url,
        webhook_secret=webhook_secret,
        runner=_default_runner,
    )

    router = APIRouter()

    @router.post("/webhook")
    async def dingtalk_webhook(request: Request):
        """DingTalk webhook handler."""
        body = await request.body()
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            raise HTTPException(status_code=400, detail="Invalid JSON")

        response = await gateway.handle_webhook(payload, request)
        return response

    @router.get("/status")
    async def dingtalk_status():
        return {
            "status": "ok",
            "platform": "dingtalk",
            "enterprise_configured": bool(gateway.app_key and gateway.app_secret),
            "group_bot_configured": bool(gateway.webhook_url),
        }

    return router


# Re-export pure formatters for single-source guard
from .dingtalk import (  # noqa: F401,E402
    escape_dingtalk_markdown,
    format_dingtalk_text_message,
    format_dingtalk_markdown_message,
    format_dingtalk_link_message,
    format_dingtalk_message,
    format_task_started,
    format_task_result,
    format_error,
    format_status,
    format_help,
)

__all__ = [
    "DingTalkGateway",
    "make_dingtalk_router",
    # Re-exported pure formatters
    "escape_dingtalk_markdown",
    "format_dingtalk_text_message",
    "format_dingtalk_markdown_message",
    "format_dingtalk_link_message",
    "format_dingtalk_message",
    "format_task_started",
    "format_task_result",
    "format_error",
    "format_status",
    "format_help",
]