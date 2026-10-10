"""veya/obase/channels/wecom.py — WeCom (Enterprise WeChat) Bot API client (oprim).

Atomic external HTTP calls. No business logic. Pure IO.
Supports both WeCom Customer Service API and Group Chat Bot webhook.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import time
import xml.etree.ElementTree as ET
from typing import Any

import httpx

from veya.obase.channels import (
    PLATFORM_WECOM,
    chunk_text,
)

_log = logging.getLogger("veya.obase.channels.wecom")

WECHAT_TOKEN_URL = "https://qyapi.weixin.qq.com/cgi-bin/gettoken"
WECHAT_SEND_URL = "https://qyapi.weixin.qq.com/cgi-bin/message/send"
WECHAT_APP_CHAT_SEND_URL = "https://qyapi.weixin.qq.com/cgi-bin/appchat/send"


class WeComClient:
    """WeCom (Enterprise WeChat) Bot API client."""

    def __init__(
        self,
        *,
        corp_id: str | None = None,
        corp_secret: str | None = None,
        agent_id: int | str | None = None,
        webhook_url: str | None = None,
        token: str | None = None,
        encoding_aes_key: str | None = None,
        timeout: float = 30.0,
        max_retries: int = 3,
    ) -> None:
        self.corp_id = corp_id
        self.corp_secret = corp_secret
        self.agent_id = str(agent_id) if agent_id else None
        self.webhook_url = webhook_url
        self.token = token
        self.encoding_aes_key = encoding_aes_key
        self._timeout = timeout
        self._max_retries = max_retries
        self._client: httpx.AsyncClient | None = None
        self._access_token: str = ""
        self._token_expiry: float = 0.0

        # Validate configuration
        has_corp = bool(corp_id and corp_secret and agent_id)
        has_webhook = bool(webhook_url)
        if not has_corp and not has_webhook:
            raise ValueError("Either (corp_id, corp_secret, agent_id) or webhook_url is required")

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout)
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def is_corp_bot(self) -> bool:
        return bool(self.corp_id and self.corp_secret and self.agent_id)

    def is_webhook_bot(self) -> bool:
        return bool(self.webhook_url)

    # ---- Corp Bot: Token management ----

    async def _get_access_token(self) -> str:
        """Get or refresh WeCom access token."""
        if not self.is_corp_bot():
            raise RuntimeError("Not configured as corp bot")

        if self._access_token and time.time() < self._token_expiry:
            return self._access_token

        last_exc = None
        for attempt in range(self._max_retries):
            try:
                resp = await self.client.get(
                    WECHAT_TOKEN_URL,
                    params={
                        "corpid": self.corp_id,
                        "corpsecret": self.corp_secret,
                    },
                )
                resp.raise_for_status()
                data = resp.json()
                if data.get("errcode") != 0:
                    raise RuntimeError(f"WeCom auth error: {data.get('errmsg')}")
                self._access_token = data["access_token"]
                expires_in = data.get("expires_in", 7200)
                self._token_expiry = time.time() + expires_in - 60
                return self._access_token
            except (httpx.RequestError, httpx.HTTPStatusError) as exc:
                last_exc = exc
                if attempt < self._max_retries - 1:
                    await asyncio.sleep(0.5 * (attempt + 1))
                    continue
                raise
        raise last_exc  # type: ignore[misc]

    # ---- Signature verification (oprim: single crypto op) ----

    @staticmethod
    def verify_signature(
        *,
        token: str,
        encoding_aes_key: str,  # unused for signature, but part of config
        payload: dict[str, Any],
        msg_signature: str,
        timestamp: str,
        nonce: str,
    ) -> bool:
        """Verify WeCom callback signature.

        WeCom: SHA1(sort(token, timestamp, nonce, encrypt))
        """
        import hashlib
        encrypt = payload.get("Encrypt", "")
        tmp_list = sorted([token, timestamp, nonce, encrypt])
        tmp_str = "".join(tmp_list)
        expected = hashlib.sha1(tmp_str.encode("utf-8")).hexdigest()
        return hmac.compare_digest(expected, msg_signature)

    # ---- Message decryption (oprim: single crypto op) ----

    def decrypt_message(
        self,
        *,
        msg_signature: str,
        timestamp: str,
        nonce: str,
        encrypt: str,
    ) -> tuple[int, str]:
        """Decrypt WeCom encrypted message.

        Returns (ret_code, decrypted_xml).
        Uses WXBizMsgCrypt algorithm (AES-256-CBC).
        """
        import base64
        from Crypto.Cipher import AES

        if not self.encoding_aes_key:
            raise RuntimeError("encoding_aes_key required for decryption")

        # Verify signature first
        payload = {"Encrypt": encrypt}
        if not self.verify_signature(
            token=self.token or "",
            encoding_aes_key=self.encoding_aes_key,
            payload=payload,
            msg_signature=msg_signature,
            timestamp=timestamp,
            nonce=nonce,
        ):
            raise ValueError("Signature verification failed")

        # Decode AES key (base64 + '=')
        aes_key = base64.b64decode(self.encoding_aes_key + "=")
        # Decrypt
        ciphertext = base64.b64decode(encrypt)
        cipher = AES.new(aes_key, AES.MODE_CBC, aes_key[:16])
        decrypted = cipher.decrypt(ciphertext)

        # Remove padding (PKCS#7)
        pad = decrypted[-1]
        if pad < 1 or pad > 32:
            raise ValueError("Invalid padding")
        decrypted = decrypted[:-pad]

        # First 16 bytes are random, next 4 bytes are msg_len (network order)
        # Then msg content, then corp_id
        msg_len = int.from_bytes(decrypted[16:20], "big")
        content = decrypted[20:20 + msg_len]
        return 0, content.decode("utf-8")

    # ---- XML parsing (oprim: pure computation) ----

    @staticmethod
    def parse_message_xml(xml_str: str) -> dict[str, Any]:
        """Parse WeCom message XML to dict."""
        try:
            root = ET.fromstring(xml_str)
        except ET.ParseError as exc:
            raise ValueError(f"Invalid XML: {exc}")

        result = {}
        for child in root:
            result[child.tag] = child.text
        return result

    @staticmethod
    def build_text_reply(
        to_user: str,
        from_user: str,
        content: str,
    ) -> str:
        """Build WeCom XML text reply."""
        return f"""<xml>
<ToUserName><![CDATA[{to_user}]]></ToUserName>
<FromUserName><![CDATA[{from_user}]]></FromUserName>
<CreateTime>{int(time.time())}</CreateTime>
<MsgType><![CDATA[text]]></MsgType>
<Content><![CDATA[{content}]]></Content>
</xml>"""

    # ---- Core send (oprim: single HTTP call) ----

    async def send_message(
        self,
        to_user: str,
        text: str,
        *,
        msgtype: str = "text",
    ) -> dict[str, Any]:
        """Send a message via WeCom Customer Service API or webhook."""
        if self.is_corp_bot():
            return await self._send_corp_message(to_user, text, msgtype)
        elif self.is_webhook_bot():
            return await self._send_webhook_message(text, msgtype)
        else:
            raise RuntimeError("No valid send path configured")

    async def _send_corp_message(
        self,
        to_user: str,
        text: str,
        msgtype: str,
    ) -> dict[str, Any]:
        """Send via corp API (Customer Service or App Chat)."""
        token = await self._get_access_token()
        payload: dict[str, Any] = {
            "touser": to_user,
            "msgtype": msgtype,
            "agentid": self.agent_id,
        }
        if msgtype == "text":
            payload["text"] = {"content": text[:600]}  # WeCom text limit
        else:
            raise ValueError(f"Unsupported msgtype: {msgtype}")

        last_exc = None
        for attempt in range(self._max_retries):
            try:
                resp = await self.client.post(
                    f"{WECHAT_SEND_URL}?access_token={token}",
                    json=payload,
                )
                resp.raise_for_status()
                data = resp.json()
                if data.get("errcode") != 0:
                    raise RuntimeError(f"WeCom send error: {data.get('errmsg')}")
                return data
            except (httpx.RequestError, httpx.HTTPStatusError) as exc:
                last_exc = exc
                if attempt < self._max_retries - 1:
                    await asyncio.sleep(0.5 * (attempt + 1))
                    continue
                raise
        raise last_exc  # type: ignore[misc]

    async def _send_webhook_message(
        self,
        text: str,
        msgtype: str,
    ) -> dict[str, Any]:
        """Send via group chat webhook."""
        if not self.webhook_url:
            raise RuntimeError("Webhook URL not configured")

        payload = {
            "msgtype": msgtype,
            msgtype: {"content": text[:2048]},
        }

        last_exc = None
        for attempt in range(self._max_retries):
            try:
                resp = await self.client.post(
                    self.webhook_url,
                    json=payload,
                    headers={"Content-Type": "application/json"},
                )
                resp.raise_for_status()
                return resp.json()
            except (httpx.RequestError, httpx.HTTPStatusError) as exc:
                last_exc = exc
                if attempt < self._max_retries - 1:
                    await asyncio.sleep(0.5 * (attempt + 1))
                    continue
                raise
        raise last_exc  # type: ignore[misc]

    async def send_chunked(
        self,
        to_user: str,
        text: str,
        *,
        msgtype: str = "text",
    ) -> list[dict[str, Any]]:
        """Send a long message as multiple chunks."""
        chunks = chunk_text(text, platform=PLATFORM_WECOM)
        results = []
        for chunk in chunks:
            results.append(await self.send_message(to_user, chunk, msgtype=msgtype))
            await asyncio.sleep(0.05)
        return results

    # ---- Bot info (oprim: single HTTP call) ----

    async def get_bot_info(self) -> dict[str, Any]:
        """Get agent info (corp bot only)."""
        if not self.is_corp_bot():
            raise RuntimeError("Not configured as corp bot")
        token = await self._get_access_token()
        resp = await self.client.get(
            "https://qyapi.weixin.qq.com/cgi-bin/agent/get",
            params={"access_token": token, "agentid": self.agent_id},
        )
        resp.raise_for_status()
        return resp.json()


# ---- Convenience factory ----

async def create_wecom_client(
    *,
    corp_id: str | None = None,
    corp_secret: str | None = None,
    agent_id: int | str | None = None,
    webhook_url: str | None = None,
    token: str | None = None,
    encoding_aes_key: str | None = None,
    timeout: float = 30.0,
) -> WeComClient:
    """Create and verify a WeCom client."""
    client = WeComClient(
        corp_id=corp_id,
        corp_secret=corp_secret,
        agent_id=agent_id,
        webhook_url=webhook_url,
        token=token,
        encoding_aes_key=encoding_aes_key,
        timeout=timeout,
    )
    if client.is_corp_bot():
        await client._get_access_token()
    return client


__all__ = [
    "WeComClient",
    "create_wecom_client",
    "WECHAT_TOKEN_URL",
    "WECHAT_SEND_URL",
    "WECHAT_APP_CHAT_SEND_URL",
]