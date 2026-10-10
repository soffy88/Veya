"""veya/oskill/im/telegram.py — Telegram Bot Gateway (Layer 4).

Uses obase.channels.TelegramClient for all IO (3O compliance).
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

from veya.obase.channels.telegram import TelegramClient, create_telegram_client
from veya.obase.channels import chunk_text, PLATFORM_TELEGRAM
from veya.oskill.im.pseudo import anonymize_user_id

logger = logging.getLogger("veya.oskill.im.telegram")

_bg_tasks: set = set()


class TelegramGateway:
    """Telegram bot gateway — webhook/polling → pseudo-anonymize → agentic_loop → reply.

    IO delegated to veya.obase.channels.TelegramClient (oprim).
    Formatting uses this module's pure formatters (oskill).
    """

    def __init__(
        self,
        *,
        bot_token: str | None = None,
        webhook_url: str | None = None,
        secret_token: str | None = None,
        runner: Callable[..., Any] | None = None,
    ):
        self.bot_token = bot_token or os.environ.get("TELEGRAM_BOT_TOKEN", "")
        self.webhook_url = webhook_url or os.environ.get("TELEGRAM_WEBHOOK_URL", "")
        self.secret_token = secret_token or os.environ.get("TELEGRAM_SECRET_TOKEN", "")
        self._runner = runner
        self._client: TelegramClient | None = None
        self._last_update_id: int = 0
        self._polling_task: asyncio.Task | None = None

    async def _get_client(self) -> TelegramClient:
        if self._client is None:
            if not self.bot_token:
                raise RuntimeError("TELEGRAM_BOT_TOKEN not configured")
            self._client = await create_telegram_client(self.bot_token)
        return self._client

    async def send_message(
        self,
        chat_id: int | str,
        text: str,
        parse_mode: str = "Markdown",
    ) -> dict | None:
        """Send a message to a Telegram chat (delegates to obase.channels)."""
        client = await self._get_client()
        return await client.send_chunked(chat_id, text, parse_mode=parse_mode)

    async def send_typing(self, chat_id: int | str) -> None:
        """Send a typing indicator."""
        client = await self._get_client()
        await client.send_chat_action(chat_id, "typing")

    async def handle_update(self, update: dict) -> dict | None:
        """Handle a single Telegram update."""
        message = update.get("message")
        if not message:
            return None

        chat = message.get("chat", {})
        chat_id = chat.get("id", 0)
        user = message.get("from", {})
        user_id = str(user.get("id", "unknown"))
        username = user.get("username", user.get("first_name", "anonymous"))
        text = message.get("text", "")

        if not text:
            return None

        pseudo_id = anonymize_user_id(f"telegram:{user_id}")

        # Handle commands
        if text.startswith("/start"):
            from .telegram import format_help
            return {
                "chat_id": chat_id,
                "text": format_help(),
                "parse_mode": "Markdown",
            }

        if text.startswith("/help"):
            from .telegram import format_help
            return {
                "chat_id": chat_id,
                "text": format_help(),
                "parse_mode": "Markdown",
            }

        if text.startswith("/status"):
            from .telegram import format_status
            return {
                "chat_id": chat_id,
                "text": format_status(username, True, pseudo_id),
                "parse_mode": "Markdown",
            }

        # Regular message — run agent
        if self._runner:
            _task_ref = asyncio.create_task(
                self._process_message(
                    chat_id,
                    text,
                    pseudo_id,
                    username,
                )
            )
            _bg_tasks.add(_task_ref)

        return None

    async def _process_message(
        self,
        chat_id: int,
        text: str,
        pseudo_id: str,
        username: str,
    ):
        """Process a user message through the agentic loop."""
        await self.send_typing(chat_id)

        try:
            result = await self._runner(text, user_ref=pseudo_id)
            content = result.get("result", "")

            if isinstance(content, list):
                text_parts = [b.get("text", "") for b in content if b.get("type") == "text"]
                content = "\n".join(text_parts) if text_parts else str(content)
            elif isinstance(content, dict):
                content = content.get("content", str(content))

            from .telegram import format_task_result
            chunks = format_task_result(username, text, str(content), result.get("cost_usd", 0))

            for chunk in chunks:
                await self.send_message(chat_id, chunk)

        except Exception as e:
            logger.error(f"Telegram agent error: {e}")
            from .telegram import format_error
            await self.send_message(chat_id, format_error(username, str(e)))

    async def handle_webhook_update(self, payload: dict) -> dict:
        """Handle a webhook-delivered update."""
        # Verify secret token if configured
        if self.secret_token:
            if payload.get("secret_token") != self.secret_token:
                raise HTTPException(status_code=401, detail="Invalid secret token")

        reply = await self.handle_update(payload)
        if reply:
            chat_id = reply.pop("chat_id", 0)
            text = reply.pop("text", "")
            parse_mode = reply.pop("parse_mode", "Markdown")
            await self.send_message(chat_id, text, parse_mode)
        return {"status": "ok"}

    async def start_polling(self, interval_sec: float = 2.0) -> None:
        """Start long-polling for Telegram updates."""
        if self._polling_task:
            return
        client = await self._get_client()
        self._polling_task = asyncio.create_task(self._poll_loop(client, interval_sec))

    async def stop_polling(self) -> None:
        if self._polling_task:
            self._polling_task.cancel()
            self._polling_task = None

    async def _poll_loop(self, client: TelegramClient, interval_sec: float) -> None:
        """Poll for updates in a loop."""
        while True:
            try:
                updates = await client.get_updates(
                    offset=self._last_update_id + 1,
                    timeout=int(interval_sec),
                )
                for update in updates:
                    self._last_update_id = update.get("update_id", 0)
                    await self.handle_update(update)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Telegram poll error: {e}")
                await asyncio.sleep(interval_sec)

    async def setup_webhook(self) -> None:
        """Set up webhook if URL configured."""
        if not self.webhook_url:
            return
        client = await self._get_client()
        await client.set_webhook(
            self.webhook_url,
            secret_token=self.secret_token or None,
            allowed_updates=["message"],
        )


# ---------------------------------------------------------------------------
# FastAPI Router
# ---------------------------------------------------------------------------


def make_telegram_router(
    bot_token: str | None = None,
    webhook_url: str | None = None,
    secret_token: str | None = None,
) -> APIRouter:
    """Build a FastAPI router for Telegram.

    Mount:  app.include_router(make_telegram_router(), prefix="/im/telegram")
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

    gateway = TelegramGateway(
        bot_token=bot_token,
        webhook_url=webhook_url,
        secret_token=secret_token,
        runner=_default_runner,
    )

    router = APIRouter()

    @router.post("/webhook")
    async def telegram_webhook(request: Request):
        """Telegram webhook handler (set via setWebhook API)."""
        body = await request.body()
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            raise HTTPException(status_code=400, detail="Invalid JSON")

        result = await gateway.handle_webhook_update(payload)
        return result

    @router.get("/status")
    async def telegram_status():
        return {
            "status": "ok",
            "platform": "telegram",
            "bot_configured": bool(gateway.bot_token),
            "webhook_configured": bool(gateway.webhook_url),
            "polling_active": gateway._polling_task is not None,
        }

    @router.post("/poll/start")
    async def telegram_start_poll(interval: float = 2.0):
        await gateway.start_polling(interval)
        return {"status": "polling_started", "interval_sec": interval}

    @router.post("/poll/stop")
    async def telegram_stop_poll():
        await gateway.stop_polling()
        return {"status": "polling_stopped"}

    @router.on_event("startup")
    async def startup():
        await gateway.setup_webhook()

    return router


# Re-export pure formatters for single-source guard
from .telegram import (  # noqa: F401,E402
    escape_markdown,
    escape_html,
    format_telegram_message,
    format_telegram_code_block,
    format_telegram_mention,
    format_task_started,
    format_task_result,
    format_error,
    format_status,
    format_help,
)

__all__ = [
    "TelegramGateway",
    "make_telegram_router",
    # Re-exported pure formatters
    "escape_markdown",
    "escape_html",
    "format_telegram_message",
    "format_telegram_code_block",
    "format_telegram_mention",
    "format_task_started",
    "format_task_result",
    "format_error",
    "format_status",
    "format_help",
]