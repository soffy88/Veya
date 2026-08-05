"""
veya/im/discord.py — Discord Bot Gateway (Layer 4).

Discord bot gateway adapter following the same pattern as feishu.py/slack.py.
Listens on Interactions Endpoint URL for slash commands and messages,
pseudo-anonymizes user IDs, and drives the agentic_loop engine.

Requires: DISCORD_BOT_TOKEN, DISCORD_APP_ID env vars.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import time
from typing import Any

try:
    from fastapi import APIRouter, HTTPException, Request, Response
    _HAS_FASTAPI = True
except ImportError:
    _HAS_FASTAPI = False
    APIRouter = object  # type: ignore
    HTTPException = Exception  # type: ignore
    Request = object  # type: ignore
    Response = object  # type: ignore

from veya.im.pseudo import anonymize_user_id

logger = logging.getLogger("veya.im.discord")

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
        """Verify Discord Ed25519 interaction signature.

        https://discord.com/developers/docs/interactions/receiving-and-responding#security-and-authorization
        """
        if not self.public_key:
            # Verification disabled — accept all (development mode)
            return True

        try:
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
            from cryptography.exceptions import InvalidSignature

            key_bytes = bytes.fromhex(self.public_key)
            public_key_obj = Ed25519PublicKey.from_public_bytes(key_bytes)
            message = timestamp.encode() + body

            sig_bytes = bytes.fromhex(signature)
            public_key_obj.verify(sig_bytes, message)
            return True
        except ImportError:
            # Fallback: naive HMAC check (less secure, requires shared secret)
            expected = hmac.new(
                self.public_key.encode(), body + timestamp.encode(), hashlib.sha256
            ).hexdigest()
            return hmac.compare_digest(expected, signature)
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
            command_name = data.get("name", "ask")

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
                asyncio.create_task(self._run_and_reply(
                    channel_id, prompt, pseudo_id, user_name
                ))

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
        self, channel_id: str, prompt: str, pseudo_id: str, user_name: str,
    ):
        """Run the agentic loop and post the result to Discord."""
        try:
            result = await self._runner(prompt, user_ref=pseudo_id)
            reply_text = self._format_result(result, user_name)
            await self._send_message(channel_id, reply_text)
        except Exception as e:
            logger.error(f"Discord agent run failed: {e}")
            await self._send_message(
                channel_id,
                f"❌ Sorry {user_name}, I encountered an error: {str(e)[:200]}",
            )

    def _format_result(self, result: dict, user_name: str) -> str:
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
            return f"**{user_name}** asked:\n> {prompt if 'prompt' in dir() else '...'}\n\n{text}"
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
    from veya.server.manifests import assemble_agentic_loop, new_session_id

    async def _default_runner(prompt: str, user_ref: str = "anon") -> dict:
        engine = assemble_agentic_loop()
        engine.run()
        try:
            result = await engine.invoke({
                "goal": prompt,
                "session_id": new_session_id(),
                "user_ref": user_ref,
            })
            return result
        finally:
            engine.stop()

    gateway = DiscordGateway(
        bot_token=bot_token,
        app_id=app_id,
        public_key=public_key,
        runner=_default_runner,
    )

    router = APIRouter()

    @router.post("/webhook")
    async def discord_webhook(request: Request):
        """Discord Interactions Endpoint webhook handler."""
        body = await request.body()

        # Verify signature
        signature = request.headers.get("X-Signature-Ed25519", "")
        timestamp = request.headers.get("X-Signature-Timestamp", "")
        if signature and timestamp and gateway.public_key:
            if not await gateway.verify_signature(body, signature, timestamp):
                raise HTTPException(status_code=401, detail="Invalid signature")

        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            raise HTTPException(status_code=400, detail="Invalid JSON")

        response = await gateway.handle_interaction(payload)
        return Response(
            content=json.dumps(response),
            media_type="application/json",
        )

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
