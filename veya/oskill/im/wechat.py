"""
veya/im/wechat.py — WeChat (微信) Bot Gateway (Layer 4).

WeChat Official Account / Enterprise WeChat gateway.
Supports: message push → pseudo-anonymize → agentic_loop → reply.

WeChat requires an Official Account (公众号) or Enterprise WeChat (企业微信)
with server-side message handling configured.

NOTE: WeChat Official Accounts can only reply to users who have sent a message
within the last 48 hours. Enterprise WeChat has no such limitation.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import time
import xml.etree.ElementTree as ET

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
("veya.im.wechat")

# ---------------------------------------------------------------------------
# WeChat API constants
# ---------------------------------------------------------------------------

WECHAT_TOKEN_URL = "https://api.weixin.qq.com/cgi-bin/token"
WECHAT_SEND_URL = "https://api.weixin.qq.com/cgi-bin/message/custom/send"
WECHAT_TEMPLATE_URL = "https://api.weixin.qq.com/cgi-bin/message/template/send"


class WeChatGateway:
    """WeChat Official Account / Enterprise WeChat gateway.

    Handles:
    - Server URL verification (GET with signature validation)
    - Incoming text messages (POST XML)
    - Pseudo-anonymization of WeChat OpenIDs
    - Agentic loop execution + reply

    Setup:
        1. Register an Official Account at https://mp.weixin.qq.com
        2. Set WECHAT_APP_ID, WECHAT_APP_SECRET env vars
        3. Set WECHAT_TOKEN for URL verification
        4. Configure server URL in WeChat backend → /im/wechat/webhook
    """

    def __init__(
        self,
        *,
        app_id: str | None = None,
        app_secret: str | None = None,
        token: str | None = None,
        encoding_aes_key: str | None = None,
        runner: callable | None = None,
    ):
        self.app_id = app_id or os.environ.get("WECHAT_APP_ID", "")
        self.app_secret = app_secret or os.environ.get("WECHAT_APP_SECRET", "")
        self.token = token or os.environ.get("WECHAT_TOKEN", "")
        self.encoding_aes_key = encoding_aes_key or os.environ.get("WECHAT_ENCODING_AES_KEY", "")
        self._access_token: str = ""
        self._token_expiry: float = 0.0
        self._runner = runner

    async def get_access_token(self) -> str:
        """Get WeChat access token."""
        if self._access_token and time.time() < self._token_expiry:
            return self._access_token

        if not self.app_id or not self.app_secret:
            raise ValueError("WECHAT_APP_ID and WECHAT_APP_SECRET required")

        try:
            import httpx

            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(
                    WECHAT_TOKEN_URL,
                    params={
                        "grant_type": "client_credential",
                        "appid": self.app_id,
                        "secret": self.app_secret,
                    },
                )
                resp.raise_for_status()
                data = resp.json()
                self._access_token = data.get("access_token", "")
                expires_in = data.get("expires_in", 7200)
                self._token_expiry = time.time() + expires_in - 60
                return self._access_token
        except Exception as e:
            logger.error(f"WeChat token error: {e}")
            raise

    def verify_url(self, signature: str, timestamp: str, nonce: str, echostr: str) -> str | None:
        """Verify WeChat server URL configuration (GET request)."""
        if not self.token:
            return None

        # Sort and SHA1
        tmp_list = sorted([self.token, timestamp, nonce])
        tmp_str = "".join(tmp_list)
        expected = hashlib.sha1(tmp_str.encode()).hexdigest()

        if signature == expected:
            return echostr
        return None

    async def handle_message(self, body: bytes) -> str:
        """Handle an incoming WeChat XML message.

        Returns:
            XML response string.
        """
        try:
            root = ET.fromstring(body)
        except ET.ParseError:
            return self._text_reply("unknown", "消息格式错误")

        msg_type = root.findtext("MsgType", "text")
        from_user = root.findtext("FromUserName", "unknown")
        to_user = root.findtext("ToUserName", "")
        content = root.findtext("Content", "")

        pseudo_id = anonymize_user_id(f"wechat:{from_user}")

        if msg_type == "text" and self._runner and content:
            # Background: run agent and reply
            _task_ref = asyncio.create_task(
                self._run_and_reply(
                    from_user,
                    to_user,
                    content,
                    pseudo_id,
                )
            )
            _bg_tasks.add(_task_ref)

        return self._text_reply(from_user, to_user, f"收到消息，正在处理: _{content[:50]}..._")

    async def _run_and_reply(
        self,
        from_user: str,
        to_user: str,
        prompt: str,
        pseudo_id: str,
    ):
        """Run agent and send reply via WeChat customer service API."""
        try:
            result = await self._runner(prompt, user_ref=pseudo_id)
            content = result.get("result", "")
            if isinstance(content, list):
                text_parts = [b.get("text", "") for b in content if b.get("type") == "text"]
                content = "\n".join(text_parts) if text_parts else str(content)
            elif isinstance(content, dict):
                content = content.get("content", str(content))
            reply_text = str(content)[:600]

            await self._send_customer_message(from_user, reply_text)
        except Exception as e:
            logger.error(f"WeChat agent run failed: {e}")
            await self._send_customer_message(from_user, f"❌ 错误: {str(e)[:100]}")

    async def _send_customer_message(self, to_user: str, content: str):
        """Send a customer service text message."""
        try:
            token = await self.get_access_token()
            import httpx

            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(
                    f"{WECHAT_SEND_URL}?access_token={token}",
                    json={
                        "touser": to_user,
                        "msgtype": "text",
                        "text": {"content": content},
                    },
                )
                if resp.status_code != 200:
                    logger.error(f"WeChat send failed: {resp.status_code} {resp.text[:200]}")
        except Exception as e:
            logger.error(f"WeChat HTTP error: {e}")

    def _text_reply(self, to_user: str, from_user: str, content: str) -> str:
        """Build a WeChat XML text reply."""
        return f"""<xml>
<ToUserName><![CDATA[{to_user}]]></ToUserName>
<FromUserName><![CDATA[{from_user}]]></FromUserName>
<CreateTime>{int(time.time())}</CreateTime>
<MsgType><![CDATA[text]]></MsgType>
<Content><![CDATA[{content}]]></Content>
</xml>"""


# ---------------------------------------------------------------------------
# FastAPI Router
# ---------------------------------------------------------------------------


def make_wechat_router(
    app_id: str | None = None,
    app_secret: str | None = None,
    token: str | None = None,
) -> APIRouter:
    """Build a FastAPI router for WeChat webhook.

    Mount:  app.include_router(make_wechat_router(), prefix="/im/wechat")
    """
    from server.coordinator_master import master_coordinator

    async def _default_runner(prompt: str, user_ref: str = "anon") -> dict:
        try:
            result = await master_coordinator.chat_stream(prompt, session_id=None, max_rounds=3)
            return {
                "status": result.get("status", "failed"),
                "content": result.get("final_answer") or result.get("error", ""),
                "cost_usd": result.get("cost_usd", 0.0),
                "user_ref": user_ref,
            }
        except Exception as exc:
            return {"status": "failed", "content": f"IM runner error: {exc}", "user_ref": user_ref}

    gateway = WeChatGateway(
        app_id=app_id,
        app_secret=app_secret,
        token=token,
        runner=_default_runner,
    )

    router = APIRouter()

    @router.get("/webhook")
    async def wechat_verify(
        signature: str = "",
        timestamp: str = "",
        nonce: str = "",
        echostr: str = "",
    ):
        """WeChat server URL verification (GET)."""
        result = gateway.verify_url(signature, timestamp, nonce, echostr)
        if result:
            return Response(content=result, media_type="text/plain")
        raise HTTPException(status_code=403, detail="Signature verification failed")

    @router.post("/webhook")
    async def wechat_message(request: Request):
        """WeChat message handler (POST XML)."""
        body = await request.body()
        xml_response = await gateway.handle_message(body)
        return Response(content=xml_response, media_type="application/xml")

    @router.get("/status")
    async def wechat_status():
        return {
            "status": "ok",
            "platform": "wechat",
            "app_configured": bool(gateway.app_id and gateway.app_secret),
            "token_configured": bool(gateway.token),
        }

    return router
