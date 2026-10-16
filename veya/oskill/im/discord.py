"""
veya/im/discord.py — Discord Bot Gateway (Layer 4).

Discord bot gateway adapter following the same pattern as feishu.py/slack.py.
Listens on Interactions Endpoint URL for slash commands and messages,
pseudo-anonymizes user IDs, and drives the agentic_loop engine.

Requires: DISCORD_BOT_TOKEN, DISCORD_APP_ID env vars.
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
("veya.im.discord")

# ---------------------------------------------------------------------------
# Discord API constants
# ---------------------------------------------------------------------------

DISCORD_API_BASE = "https://discord.com/api/v10"
DISCORD_CMD_CREATE_MESSAGE = f"{DISCORD_API_BASE}/channels/{{channel_id}}/messages"

# ---------------------------------------------------------------------------
# Discord Gateway
# ---------------------------------------------------------------------------


class DiscordGateway:
    """Discord bot gateway — Interactions Endpoint → agentic_loop → reply.

    Handles:
    - Ping/Pong (Discord interaction verification)
    - Slash commands (e.g., /ask <prompt>)
    - Message commands (right-click → ask veya)
    - Ed25519 signature verification (X-Signature-Ed25519 header)
    - Pseudo-anonymization of Discord user IDs (Guild + User isolation)

    Setup:
        1. Create a Discord App at https://discord.com/developers/applications
        2. Set DISCORD_BOT_TOKEN and DISCORD_APP_ID env vars
        3. Set Interactions Endpoint URL to your server + /im/discord/webhook
        4. (Optional) Set DISCORD_PUBLIC_KEY for signature verification
    """

    def __init__(
        self,
        *,
        bot_token: str | None = None,
        app_id: str | None = None,
        public_key: str | None = None,
        runner: callable | None = None,
        reply_fn: callable | None = None,
    ):
        self.bot_token = bot_token or os.environ.get("DISCORD_BOT_TOKEN", "")
        self.app_id = app_id or os.environ.get("DISCORD_APP_ID", "")
        self.public_key = public_key or os.environ.get("DISCORD_PUBLIC_KEY", "")
        self._runner = runner
        self._reply_fn = reply_fn

    async def verify_signature(self, body: bytes, signature: str, timestamp: str) -> bool:
        """Verify Discord Ed25519 signatures; never fall back to an unrelated MAC."""
        if not self.public_key or not signature or not timestamp:
            return False
        try:
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

            key_bytes = bytes.fromhex(self.public_key)
            sig_bytes = bytes.fromhex(signature)
            Ed25519PublicKey.from_public_bytes(key_bytes).verify(
                sig_bytes, timestamp.encode("utf-8") + body
            )
            return True
        except Exception:
            return False

    async def handle_interaction(self, payload: dict) -> dict:
        """Handle a Discord interaction (slash command, message command, etc.).

        Returns:
            Discord interaction response dict.
        """
        interaction_type = payload.get("type", 0)

        # Type 1: PING (Discord verification)
        if interaction_type == 1:
            return {"type": 1}  # PONG

        # Type 2: APPLICATION_COMMAND
        if interaction_type == 2:
            data = payload.get("data", {})

            # Extract user info
            user = payload.get("member", {}).get("user", {}) or payload.get("user", {})
            user_id = str(user.get("id", "unknown"))
            user_name = user.get("username", "anonymous")
            pseudo_id = anonymize_user_id(f"discord:{user_id}")

            # Extract options
            options = data.get("options", [])
            prompt = ""
            for opt in options:
                if opt.get("name") == "prompt":
                    prompt = opt.get("value", "")
            if not prompt and "prompt" in data:
                prompt = data["prompt"]

            # Acknowledge immediately (Discord requires response within 3s)
            channel_id = payload.get("channel_id", "")

            if self._runner and prompt:
                # Background task: run agent and reply
                _task_ref = asyncio.create_task(
                    self._run_and_reply(channel_id, prompt, pseudo_id, user_name)
                )
                _bg_tasks.add(_task_ref)

            # Return deferred response
            return {
                "type": 4,  # CHANNEL_MESSAGE_WITH_SOURCE
                "data": {
                    "content": f"🤖 Thinking about: _{prompt[:100]}..._",
                },
            }

        # Type 3: MESSAGE_COMPONENT (button clicks, select menus)
        if interaction_type == 3:
            return {
                "type": 4,
                "data": {"content": "Component interaction received."},
            }

        return {"type": 4, "data": {"content": "Unknown interaction type."}}

    async def _run_and_reply(
        self,
        channel_id: str,
        prompt: str,
        pseudo_id: str,
        user_name: str,
    ):
        """Run the agentic loop and post the result to Discord."""
        try:
            result = await self._runner(prompt, user_ref=pseudo_id)
            reply_text = self._format_result(result, user_name, prompt)
            await self._send_message(channel_id, reply_text)
        except Exception as e:
            logger.error(f"Discord agent run failed: {e}")
            await self._send_message(
                channel_id,
                f"❌ Sorry {user_name}, I encountered an error: {str(e)[:200]}",
            )

    def _format_result(self, result: dict, user_name: str, prompt: str = "") -> str:
        """Format agent result for Discord message."""
        status = result.get("status", "completed")
        content = result.get("result", "") or result.get("turn_result", {}).get("content", "")

        if isinstance(content, list):
            text_parts = [b.get("text", "") for b in content if b.get("type") == "text"]
            content = "\n".join(text_parts) if text_parts else str(content)
        elif isinstance(content, dict):
            content = content.get("content", str(content))

        text = str(content)[:1900]  # Discord message limit
        if status == "completed":
            return f"**{user_name}** asked:\n> {prompt or '...'}\n\n{text}"
        else:
            return f"❌ Task failed ({status}): {text[:500]}"

    async def _send_message(self, channel_id: str, content: str):
        """Send a message to a Discord channel via Bot API."""
        if not self.bot_token:
            logger.warning("No DISCORD_BOT_TOKEN set; cannot send message")
            return

        try:
            import httpx

            url = DISCORD_CMD_CREATE_MESSAGE.format(channel_id=channel_id)
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(
                    url,
                    headers={
                        "Authorization": f"Bot {self.bot_token}",
                        "Content-Type": "application/json",
                    },
                    json={"content": content[:2000]},
                )
                if resp.status_code != 200:
                    logger.error(f"Discord send failed: {resp.status_code} {resp.text[:200]}")
        except Exception as e:
            logger.error(f"Discord HTTP error: {e}")


# ---------------------------------------------------------------------------
# FastAPI Router
# ---------------------------------------------------------------------------


def make_discord_router(
    bot_token: str | None = None,
    app_id: str | None = None,
    public_key: str | None = None,
) -> APIRouter:
    """Build a FastAPI router for Discord Interactions Endpoint.

    Mount it in your app:  app.include_router(make_discord_router(), prefix="/im/discord")
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

    gateway = DiscordGateway(
        bot_token=bot_token,
        app_id=app_id,
        public_key=public_key,
        runner=_default_runner,
    )

    router = APIRouter()

    @router.post("/webhook")
    async def discord_webhook(request: Request):
        """Accept an interaction only after mandatory Ed25519 verification."""
        body = await request.body()
        signature = request.headers.get("X-Signature-Ed25519", "")
        timestamp = request.headers.get("X-Signature-Timestamp", "")
        if not gateway.public_key:
            raise HTTPException(status_code=503, detail="Discord public key is not configured")
        if not signature or not timestamp:
            raise HTTPException(status_code=401, detail="Missing Discord signature headers")
        if not await gateway.verify_signature(body, signature, timestamp):
            raise HTTPException(status_code=401, detail="Invalid Discord signature")
        try:
            payload = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise HTTPException(status_code=400, detail="Invalid JSON")
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="Invalid JSON object")
        response = await gateway.handle_interaction(payload)
        return Response(content=json.dumps(response), media_type="application/json")

    # Bot status endpoint
    @router.get("/status")
    async def discord_status():
        return {
            "status": "ok",
            "platform": "discord",
            "app_id": gateway.app_id,
            "bot_configured": bool(gateway.bot_token),
        }

    return router
