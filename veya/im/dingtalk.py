"""
veya/im/dingtalk.py — DingTalk (钉钉) Bot Gateway (Layer 4).

DingTalk robot gateway — webhook → pseudo-anonymize → agentic_loop → reply.
Supports: outgoing webhook (企业机器人) + incoming webhook (群机器人).

Requires: DINGTALK_APP_KEY, DINGTALK_APP_SECRET env vars for enterprise bots,
          or DINGTALK_WEBHOOK_URL for group chat bots.
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
from urllib.parse import quote_plus

try:
    from fastapi import APIRouter, HTTPException, Request
    _HAS_FASTAPI = True
except ImportError:
    _HAS_FASTAPI = False
    APIRouter = object  # type: ignore
    HTTPException = Exception  # type: ignore
    Request = object  # type: ignore

from veya.im.pseudo import anonymize_user_id

logger = logging.getLogger("veya.im.dingtalk")

# ---------------------------------------------------------------------------
# DingTalk API constants
# ---------------------------------------------------------------------------

DINGTALK_OAUTH_URL = "https://api.dingtalk.com/v1.0/oauth2/accessToken"
DINGTALK_SEND_URL = "https://api.dingtalk.com/v1.0/robot/oToMessages/batchSend"
DINGTALK_GROUP_SEND_URL = "https://oapi.dingtalk.com/robot/send"  # group chat bot


class DingTalkGateway:
    """DingTalk robot gateway.

    Two modes:
    1. **Enterprise Bot** (企业机器人): Uses OAuth 2.0 app credentials.
       Handles outgoing webhooks from DingTalk Open Platform.
    2. **Group Chat Bot** (群机器人): Uses webhook URL + secret for
       incoming messages to group chats.

    Setup:
        Enterprise: Set DINGTALK_APP_KEY, DINGTALK_APP_SECRET
        Group Chat: Set DINGTALK_WEBHOOK_URL, DINGTALK_WEBHOOK_SECRET
    """

    def __init__(
        self,
        *,
        app_key: str | None = None,
        app_secret: str | None = None,
        webhook_url: str | None = None,
        webhook_secret: str | None = None,
        runner: callable | None = None,
    ):
        self.app_key = app_key or os.environ.get("DINGTALK_APP_KEY", "")
        self.app_secret = app_secret or os.environ.get("DINGTALK_APP_SECRET", "")
        self.webhook_url = webhook_url or os.environ.get("DINGTALK_WEBHOOK_URL", "")
        self.webhook_secret = webhook_secret or os.environ.get("DINGTALK_WEBHOOK_SECRET", "")
        self._access_token: str = ""
        self._token_expiry: float = 0.0
        self._runner = runner

    async def get_access_token(self) -> str:
        """Get or refresh DingTalk OAuth access token."""
        if self._access_token and time.time() < self._token_expiry:
            return self._access_token

        if not self.app_key or not self.app_secret:
            raise ValueError("DINGTALK_APP_KEY and DINGTALK_APP_SECRET required")

        try:
            import httpx

            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(
                    DINGTALK_OAUTH_URL,
                    json={"appKey": self.app_key, "appSecret": self.app_secret},
                    headers={"Content-Type": "application/json"},
                )
                resp.raise_for_status()
                data = resp.json()
                self._access_token = data.get("accessToken", "")
                # Token typically expires in 7200s
                expires_in = data.get("expireIn", 7200)
                self._token_expiry = time.time() + expires_in - 60  # buffer
                return self._access_token
        except Exception as e:
            logger.error(f"DingTalk OAuth failed: {e}")
            raise

    def verify_signature(self, timestamp: str, sign: str) -> bool:
        """Verify DingTalk group chat bot webhook signature.

        DingTalk signs: timestamp + "\n" + secret → HMAC-SHA256 → base64
        """
        if not self.webhook_secret:
            return True  # No verification configured

        string_to_sign = f"{timestamp}\n{self.webhook_secret}"
        expected = hmac.new(
            self.webhook_secret.encode(),
            string_to_sign.encode(),
            hashlib.sha256,
        ).digest()
        import base64
        expected_b64 = base64.b64encode(expected).decode()
        return hmac.compare_digest(expected_b64, sign)

    async def handle_webhook(self, payload: dict) -> dict:
        """Handle a DingTalk webhook message.

        Returns:
            Response dict to send back.
        """
        # Extract message
        text_content = ""
        sender_id = "unknown"

        # Group chat bot format
        if "text" in payload:
            text_content = payload.get("text", {}).get("content", "")
            sender_id = payload.get("senderId", "unknown")
        # Enterprise bot format
        elif "msgContent" in payload:
            text_content = payload.get("msgContent", "")
            sender_id = payload.get("senderStaffId", "unknown")

        session_webhook = payload.get("sessionWebhook", "")

        user_id = f"dingtalk:{sender_id}"
        pseudo_id = anonymize_user_id(user_id)

        if self._runner and text_content:
            # Background: run agent and reply
            asyncio.create_task(self._run_and_reply(
                text_content, session_webhook, pseudo_id, sender_id,
            ))

        return {
            "msgtype": "text",
            "text": {"content": f"收到消息，正在处理: _{text_content[:50]}..._"},
        }

    async def _run_and_reply(
        self, prompt: str, session_webhook: str, pseudo_id: str, sender_id: str,
    ):
        """Run agent and send reply."""
        try:
            result = await self._runner(prompt, user_ref=pseudo_id)
            status = result.get("status", "completed")
            content = result.get("result", "")
            if isinstance(content, list):
                text_parts = [b.get("text", "") for b in content if b.get("type") == "text"]
                content = "\n".join(text_parts) if text_parts else str(content)
            elif isinstance(content, dict):
                content = content.get("content", str(content))
            reply_text = str(content)[:5000]

            await self._send_reply(session_webhook, reply_text if status == "completed" else f"❌ {reply_text[:500]}")
        except Exception as e:
            logger.error(f"DingTalk agent run failed: {e}")
            await self._send_reply(session_webhook, f"❌ 错误: {str(e)[:200]}")

    async def _send_reply(self, session_webhook: str, content: str):
        """Send a reply via DingTalk webhook."""
        if not session_webhook:
            return

        try:
            import httpx

            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(
                    session_webhook,
                    json={
                        "msgtype": "text",
                        "text": {"content": content},
                    },
                    headers={"Content-Type": "application/json"},
                )
                if resp.status_code != 200:
                    logger.error(f"DingTalk reply failed: {resp.status_code}")
        except Exception as e:
            logger.error(f"DingTalk HTTP error: {e}")

    async def _send_group_reply(self, content: str):
        """Send a reply to a DingTalk group via webhook."""
        if not self.webhook_url:
            return

        try:
            import httpx

            timestamp = str(int(time.time() * 1000))
            sign = ""
            if self.webhook_secret:
                string_to_sign = f"{timestamp}\n{self.webhook_secret}"
                sign = hmac.new(
                    self.webhook_secret.encode(),
                    string_to_sign.encode(),
                    hashlib.sha256,
                ).digest()
                import base64
                sign = base64.b64encode(sign).decode()
                sign = quote_plus(sign)

            full_url = f"{self.webhook_url}&timestamp={timestamp}&sign={sign}"

            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(
                    full_url,
                    json={
                        "msgtype": "text",
                        "text": {"content": content[:20000]},
                    },
                    headers={"Content-Type": "application/json"},
                )
        except Exception as e:
            logger.error(f"DingTalk group reply error: {e}")


# ---------------------------------------------------------------------------
# FastAPI Router
# ---------------------------------------------------------------------------


def make_dingtalk_router(
    app_key: str | None = None,
    app_secret: str | None = None,
    webhook_url: str | None = None,
    webhook_secret: str | None = None,
) -> APIRouter:
    """Build a FastAPI router for DingTalk webhook.

    Mount:  app.include_router(make_dingtalk_router(), prefix="/im/dingtalk")
    """
    from veya.server.manifests import assemble_agentic_loop, new_session_id

    async def _default_runner(prompt: str, user_ref: str = "anon") -> dict:
        engine = assemble_agentic_loop()
        engine.run()
        try:
            return await engine.invoke({
                "goal": prompt,
                "session_id": new_session_id(),
                "user_ref": user_ref,
            })
        finally:
            engine.stop()

    gateway = DingTalkGateway(
        app_key=app_key, app_secret=app_secret,
        webhook_url=webhook_url, webhook_secret=webhook_secret,
        runner=_default_runner,
    )

    router = APIRouter()

    @router.post("/webhook")
    async def dingtalk_webhook(request: Request):
        """DingTalk webhook handler."""
        body = await request.body()

        # Verify group chat bot signature
        timestamp = request.headers.get("timestamp", "")
        sign = request.headers.get("sign", "")
        if timestamp and sign and gateway.webhook_secret:
            if not gateway.verify_signature(timestamp, sign):
                raise HTTPException(status_code=401, detail="Invalid signature")

        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            raise HTTPException(status_code=400, detail="Invalid JSON")

        response = await gateway.handle_webhook(payload)
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
