"""
veya/im/telegram.py — Telegram Bot Gateway (Layer 4).

Wires the 3O obase Telegram client into the veya IM gateway layer.
Uses long-polling (getUpdates) for simplicity — no webhook needed.

Requires: TELEGRAM_BOT_TOKEN env var.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os

try:
    from fastapi import APIRouter, HTTPException, Request, Response
    _HAS_FASTAPI = True
except ImportError:
    _HAS_FASTAPI = False
    APIRouter = object  # type: ignore
    HTTPException = Exception  # type: ignore
    Request = object  # type: ignore
    Response = object  # type: ignore

from veya.oskill.im.pseudo import anonymize_user_id

logger = logging.getLogger

_bg_tasks: set = set()
("veya.im.telegram")

TELEGRAM_API_BASE = "https://api.telegram.org"


class TelegramGateway:
    """Telegram bot gateway — long-polling + agentic_loop.

    Handles:
    - Text messages → pseudo-anonymize → agentic_loop → reply
    - /start, /help commands
    - Typing indicators and markdown formatting

    Setup:
        1. Create a bot with @BotFather → get token
        2. Set TELEGRAM_BOT_TOKEN env var
        3. Call /im/telegram/webhook to receive updates (or use polling)
        4. (Optional) Set TELEGRAM_WEBHOOK_URL for webhook mode
    """

    def __init__(
        self,
        *,
        bot_token: str | None = None,
        webhook_url: str | None = None,
        runner: callable | None = None,
    ):
        self.bot_token = bot_token or os.environ.get("TELEGRAM_BOT_TOKEN", "")
        self.webhook_url = webhook_url or os.environ.get("TELEGRAM_WEBHOOK_URL", "")
        self._runner = runner
        self._last_update_id: int = 0
        self._polling_task: asyncio.Task | None = None

    async def send_message(
        self, chat_id: int | str, text: str, parse_mode: str = "Markdown",
    ) -> dict | None:
        """Send a message to a Telegram chat."""
        if not self.bot_token:
            return None

        try:
            import httpx

            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(
                    f"{TELEGRAM_API_BASE}/bot{self.bot_token}/sendMessage",
                    json={
                        "chat_id": chat_id,
                        "text": text[:4096],  # Telegram limit
                        "parse_mode": parse_mode,
                    },
                )
                resp.raise_for_status()
                return resp.json()
        except Exception as e:
            logger.error(f"Telegram send error: {e}")
            return None

    async def send_typing(self, chat_id: int | str):
        """Send a typing indicator."""
        if not self.bot_token:
            return
        try:
            import httpx
            async with httpx.AsyncClient(timeout=10) as client:
                await client.post(
                    f"{TELEGRAM_API_BASE}/bot{self.bot_token}/sendChatAction",
                    json={"chat_id": chat_id, "action": "typing"},
                )
        except Exception:
            pass

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
            return {
                "chat_id": chat_id,
                "text": (
                    f"👋 Hello {username}! I'm **veya**, an AI coding agent.\n\n"
                    f"Send me any question or task, and I'll help you.\n\n"
                    f"*Example:* 'Fix the bug in auth.py'\n"
                    f"*Example:* 'Write a REST API for user management'\n"
                    f"*Example:* 'Review the latest commit'"
                ),
                "parse_mode": "Markdown",
            }

        if text.startswith("/help"):
            return {
                "chat_id": chat_id,
                "text": (
                    "**veya Help**\n\n"
                    "• Send any message to start a task\n"
                    "• /status — check bot status\n"
                    "• /history — view recent tasks\n"
                    "• /reset — clear conversation context"
                ),
                "parse_mode": "Markdown",
            }

        if text.startswith("/status"):
            return {
                "chat_id": chat_id,
                "text": (
                    f"✅ **veya** is running.\n"
                    f"• Session: active\n"
                    f"• User: {username} ({pseudo_id})\n"
                    f"• Gateway: Telegram"
                ),
                "parse_mode": "Markdown",
            }

        # Regular message — run agent
        if self._runner:
            _task_ref = asyncio.create_task(self._process_message(
                chat_id, text, pseudo_id, username,
            ))
            _bg_tasks.add(_task_ref)

        return None

    async def _process_message(
        self, chat_id: int, text: str, pseudo_id: str, username: str,
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

            reply = str(content)[:3800]
            cost = result.get("cost_usd", 0)

            final = (
                f"**{username}** asked:\n"
                f"> _{text[:200]}..._\n\n"
                f"{reply}\n\n"
                f"💰 _${cost:.4f}_"
            )
            await self.send_message(chat_id, final)

        except Exception as e:
            logger.error(f"Telegram agent error: {e}")
            await self.send_message(
                chat_id,
                f"❌ Error: _{str(e)[:200]}_",
            )

    async def handle_webhook_update(self, payload: dict) -> dict:
        """Handle a webhook-delivered update."""
        reply = await self.handle_update(payload)
        if reply:
            chat_id = reply.pop("chat_id", 0)
            text = reply.pop("text", "")
            parse_mode = reply.pop("parse_mode", "Markdown")
            await self.send_message(chat_id, text, parse_mode)
        return {"status": "ok"}

    async def start_polling(self, interval_sec: float = 2.0):
        """Start long-polling for Telegram updates."""
        if self._polling_task:
            return
        self._polling_task = _task_ref = asyncio.create_task(self._poll_loop(interval_sec))

    async def stop_polling(self):
        if self._polling_task:
            self._polling_task.cancel()
            self._polling_task = None

    async def _poll_loop(self, interval_sec: float):
        """Poll for updates in a loop."""
        import httpx

        while True:
            try:
                async with httpx.AsyncClient(timeout=30) as client:
                    resp = await client.get(
                        f"{TELEGRAM_API_BASE}/bot{self.bot_token}/getUpdates",
                        params={
                            "offset": self._last_update_id + 1,
                            "timeout": int(interval_sec),
                        },
                    )
                    if resp.status_code == 200:
                        data = resp.json()
                        for update in data.get("result", []):
                            self._last_update_id = update.get("update_id", 0)
                            await self.handle_update(update)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Telegram poll error: {e}")
                await asyncio.sleep(interval_sec)


# ---------------------------------------------------------------------------
# FastAPI Router
# ---------------------------------------------------------------------------


def make_telegram_router(
    bot_token: str | None = None,
    webhook_url: str | None = None,
) -> APIRouter:
    """Build a FastAPI router for Telegram.

    Mount:  app.include_router(make_telegram_router(), prefix="/im/telegram")
    """
    from server.coordinator_master import master_coordinator

    async def _default_runner(prompt: str, user_ref: str = "anon") -> dict:
        try:
            result = await master_coordinator.chat_stream(prompt, session_id=None, max_rounds=3)
            return {"status": result.get("status", "failed"), "content": result.get("final_answer") or result.get("error", ""), "cost_usd": result.get("cost_usd", 0.0), "user_ref": user_ref}
        except Exception as exc:
            return {"status": "failed", "content": f"IM runner error: {exc}", "user_ref": user_ref}

    gateway = TelegramGateway(
        bot_token=bot_token, webhook_url=webhook_url,
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

    return router
