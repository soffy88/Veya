"""veya/oskill/im/wecom.py — WeCom (Enterprise WeChat) Bot Gateway (Layer 4).

Uses obase.channels.WeComClient for all IO (3O compliance).
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
    from fastapi import APIRouter, HTTPException, Request, Response

    _HAS_FASTAPI = True
except ImportError:
    _HAS_FASTAPI = False
    APIRouter = object  # type: ignore
    HTTPException = Exception  # type: ignore
    Request = object  # type: ignore
    Response = object  # type: ignore

from veya.obase.channels.wecom import WeComClient, create_wecom_client
from veya.obase.channels import PLATFORM_WECOM
from veya.oskill.im.pseudo import anonymize_user_id

logger = logging.getLogger("veya.oskill.im.wecom")

_bg_tasks: set = set()


class WeComGateway:
    """WeCom (Enterprise WeChat) gateway.

    IO delegated to veya.obase.channels.WeComClient (oprim).
    Formatting uses this module's pure formatters (oskill).
    """

    def __init__(
        self,
        *,
        corp_id: str | None = None,
        corp_secret: str | None = None,
        agent_id: int | str | None = None,
        webhook_url: str | None = None,
        token: str | None = None,
        encoding_aes_key: str | None = None,
        runner: Callable[..., Any] | None = None,
    ):
        self.corp_id = corp_id or os.environ.get("WECOM_CORP_ID", "")
        self.corp_secret = corp_secret or os.environ.get("WECOM_CORP_SECRET", "")
        self.agent_id = str(agent_id) if agent_id else os.environ.get("WECOM_AGENT_ID", "")
        self.webhook_url = webhook_url or os.environ.get("WECOM_WEBHOOK_URL", "")
        self.token = token or os.environ.get("WECOM_TOKEN", "")
        self.encoding_aes_key = encoding_aes_key or os.environ.get("WECOM_ENCODING_AES_KEY", "")
        self._runner = runner
        self._client: WeComClient | None = None

    async def _get_client(self) -> WeComClient:
        if self._client is None:
            self._client = await create_wecom_client(
                corp_id=self.corp_id or None,
                corp_secret=self.corp_secret or None,
                agent_id=self.agent_id or None,
                webhook_url=self.webhook_url or None,
                token=self.token or None,
                encoding_aes_key=self.encoding_aes_key or None,
            )
        return self._client

    async def verify_url(self, signature: str, timestamp: str, nonce: str, echostr: str) -> str | None:
        """Verify WeCom server URL configuration (GET request)."""
        import hashlib
        if not self.token:
            return None
        tmp_list = sorted([self.token, timestamp, nonce])
        tmp_str = "".join(tmp_list)
        expected = hashlib.sha1(tmp_str.encode()).hexdigest()
        if signature == expected:
            return echostr
        return None

    async def handle_message(self, body: bytes, request: Request | None = None) -> str:
        """Handle an incoming WeCom XML message.

        Returns XML response string.
        """
        # Verify signature if headers present
        if request:
            msg_signature = request.query_params.get("msg_signature", "")
            timestamp = request.query_params.get("timestamp", "")
            nonce = request.query_params.get("nonce", "")
            if msg_signature and timestamp and nonce:
                from veya.obase.channels import verify_wecom_signature
                try:
                    payload = json.loads(body)
                except json.JSONDecodeError:
                    # Try XML parsing
                    import xml.etree.ElementTree as ET
                    root = ET.fromstring(body)
                    encrypt = root.findtext("Encrypt", "")
                    payload = {"Encrypt": encrypt}
                if not verify_wecom_signature(
                    token=self.token,
                    encoding_aes_key=self.encoding_aes_key,
                    payload=payload,
                    msg_signature=msg_signature,
                    timestamp=timestamp,
                    nonce=nonce,
                ):
                    raise HTTPException(status_code=403, detail="Signature verification failed")

        try:
            import xml.etree.ElementTree as ET
            root = ET.fromstring(body)
        except ET.ParseError:
            from .wecom import build_text_reply
            return build_text_reply("unknown", "消息格式错误")

        msg_type = root.findtext("MsgType", "text")
        from_user = root.findtext("FromUserName", "unknown")
        to_user = root.findtext("ToUserName", "")
        content = root.findtext("Content", "")

        pseudo_id = anonymize_user_id(f"wecom:{from_user}")

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

        from .wecom import format_task_started
        xml_reply = format_task_started(from_user, to_user, f"收到消息，正在处理: {content[:50]}...")
        return xml_reply

    async def _run_and_reply(
        self,
        from_user: str,
        to_user: str,
        prompt: str,
        pseudo_id: str,
    ):
        """Run agent and send reply via WeCom customer service API."""
        client = await self._get_client()
        try:
            result = await self._runner(prompt, user_ref=pseudo_id)
            content = result.get("result", "")
            if isinstance(content, list):
                text_parts = [b.get("text", "") for b in content if b.get("type") == "text"]
                content = "\n".join(text_parts) if text_parts else str(content)
            elif isinstance(content, dict):
                content = content.get("content", str(content))

            from .wecom import format_task_result, format_error
            if result.get("status") == "completed":
                chunks = format_task_result(from_user, prompt, str(content), result.get("cost_usd", 0.0))
            else:
                chunks = format_error(from_user, str(content))

            for chunk in chunks:
                await client.send_message(from_user, chunk, msgtype="markdown")
        except Exception as e:
            logger.error(f"WeCom agent run failed: {e}")
            from .wecom import format_error
            chunk = format_error(from_user, str(e))
            await client.send_message(from_user, chunk, msgtype="markdown")


def make_wecom_router(
    corp_id: str | None = None,
    corp_secret: str | None = None,
    agent_id: int | str | None = None,
    webhook_url: str | None = None,
    token: str | None = None,
    encoding_aes_key: str | None = None,
) -> APIRouter:
    """Build a FastAPI router for WeCom webhook.

    Mount:  app.include_router(make_wecom_router(), prefix="/im/wecom")
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

    gateway = WeComGateway(
        corp_id=corp_id,
        corp_secret=corp_secret,
        agent_id=agent_id,
        webhook_url=webhook_url,
        token=token,
        encoding_aes_key=encoding_aes_key,
        runner=_default_runner,
    )

    router = APIRouter()

    @router.get("/webhook")
    async def wecom_verify(
        signature: str = "",
        timestamp: str = "",
        nonce: str = "",
        echostr: str = "",
    ):
        """WeCom server URL verification (GET)."""
        result = await gateway.verify_url(signature, timestamp, nonce, echostr)
        if result:
            return Response(content=result, media_type="text/plain")
        raise HTTPException(status_code=403, detail="Signature verification failed")

    @router.post("/webhook")
    async def wecom_message(request: Request):
        """WeCom message handler (POST XML/JSON)."""
        body = await request.body()
        xml_response = await gateway.handle_message(body, request)
        return Response(content=xml_response, media_type="application/xml")

    @router.get("/status")
    async def wecom_status():
        return {
            "status": "ok",
            "platform": "wecom",
            "corp_configured": bool(gateway.corp_id and gateway.corp_secret and gateway.agent_id),
            "webhook_configured": bool(gateway.webhook_url),
        }

    return router


# Re-export pure formatters for single-source guard
from .wecom import (  # noqa: F401,E402
    format_wecom_text_message,
    format_wecom_markdown_message,
    format_wecom_news_message,
    format_wecom_message,
    format_task_started,
    format_task_result,
    format_error,
    format_status,
    format_help,
)

__all__ = [
    "WeComGateway",
    "make_wecom_router",
    # Re-exported pure formatters
    "format_wecom_text_message",
    "format_wecom_markdown_message",
    "format_wecom_news_message",
    "format_wecom_message",
    "format_task_started",
    "format_task_result",
    "format_error",
    "format_status",
    "format_help",
]