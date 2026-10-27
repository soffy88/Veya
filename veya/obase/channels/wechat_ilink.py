"""veya/obase/channels/wechat_ilink.py — Tencent iLink protocol client (oprim).

Personal-WeChat edge protocol, adapted from the method in Cindy's
``wechat-ilink`` package (MIT, itself adapted from Tencent's own
``openclaw-weixin``): QR authorization, long polling, text sending.

Hard rules carried over from that package:
- Only credential-free HTTPS origins under ``weixin.qq.com`` are accepted,
  for the base URL and for any QR URL the server returns.
- Errors never include response bodies, authorization headers, QR polling
  tokens, or bot tokens.
- This module owns wire calls only. Credential/session persistence belongs
  to the host (the poller script keeps its session file 0600).

This is an UNOFFICIAL personal-account protocol. Using it can get the
WeChat account restricted. That risk is the account owner's call; the code
itself stays fail-closed (bad QR origin, bad status, or missing session all
refuse instead of proceeding).
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlparse

import httpx

_log = logging.getLogger("veya.obase.channels.wechat_ilink")

ILINK_BASE_URL = "https://ilinkai.weixin.qq.com"

#: QR poll outcomes, mirroring the iLink status vocabulary.
QR_STATUSES = (
    "wait",
    "scaned",
    "confirmed",
    "expired",
    "scaned_but_redirect",
    "need_verifycode",
    "verify_code_blocked",
    "binded_redirect",
)


class WechatIlinkError(RuntimeError):
    """Sanitized iLink failure: code + short message, never payloads/tokens."""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(f"[{code}] {message or code}")
        self.code = code


def validate_weixin_https_url(raw: str) -> str:
    """Accept only credential-free HTTPS URLs under weixin.qq.com."""

    parsed = urlparse(raw or "")
    if (
        parsed.scheme != "https"
        or parsed.username
        or parsed.password
        or not (parsed.hostname == "weixin.qq.com" or (parsed.hostname or "").endswith(".weixin.qq.com"))
    ):
        raise WechatIlinkError("BAD_URL", "not a weixin.qq.com HTTPS URL")
    return raw


def validate_base_url(raw: str) -> str:
    """The iLink base URL must be exactly the Tencent host, nothing else."""

    if (raw or "").rstrip("/") != ILINK_BASE_URL:
        raise WechatIlinkError("BAD_URL", "unexpected iLink base URL")
    return ILINK_BASE_URL


class WechatIlinkClient:
    """Thin iLink client: QR begin/poll, message long-poll, text send."""

    def __init__(
        self,
        *,
        base_url: str = ILINK_BASE_URL,
        bot_agent: str = "Veya/1.0",
        timeout: float = 30.0,
        long_poll_timeout: float = 60.0,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = validate_base_url(base_url)
        self.bot_agent = bot_agent
        self._timeout = timeout
        self._long_poll_timeout = long_poll_timeout
        self._client = http_client
        self.bot_token: str | None = None
        self.ilink_bot_id: str | None = None
        self.ilink_user_id: str | None = None

    @property
    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout)
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _headers(self, *, with_token: bool = False) -> dict[str, str]:
        headers = {"User-Agent": self.bot_agent, "Content-Type": "application/json"}
        if with_token and self.bot_token:
            headers["Authorization"] = f"Bearer {self.bot_token}"
        return headers

    async def _post(self, path: str, payload: dict[str, Any], *, with_token: bool = False) -> dict[str, Any]:
        try:
            resp = await self._http.post(
                self.base_url + path,
                headers=self._headers(with_token=with_token),
                json=payload,
            )
            resp.raise_for_status()
            data = resp.json()
        except (httpx.RequestError, httpx.HTTPStatusError) as exc:
            raise WechatIlinkError("TRANSPORT", "iLink request failed") from exc
        except ValueError as exc:
            raise WechatIlinkError("BAD_RESPONSE", "iLink returned bad JSON") from exc
        if not isinstance(data, dict):
            raise WechatIlinkError("BAD_RESPONSE", "iLink returned a non-object")
        return data

    async def begin_qr(self) -> dict[str, str]:
        """Start QR authorization. Returns the QR URL + poll token (never logged)."""

        data = await self._post("/api/qrcode/begin", {})
        qr_url = data.get("qrcode_img_content")
        qrcode = data.get("qrcode")
        if not isinstance(qr_url, str) or not isinstance(qrcode, str) or not qrcode:
            raise WechatIlinkError("BAD_RESPONSE", "iLink omitted QR fields")
        return {"qr_url": validate_weixin_https_url(qr_url), "qrcode": qrcode}

    async def poll_qr(self, qrcode: str) -> dict[str, Any]:
        """Poll one QR challenge. On confirmed, stores the session in memory."""

        data = await self._post("/api/qrcode/status", {"qrcode": qrcode})
        status = data.get("status")
        if status not in QR_STATUSES:
            raise WechatIlinkError("BAD_RESPONSE", "unknown QR status")
        if status == "confirmed":
            token = data.get("bot_token")
            if not isinstance(token, str) or not token:
                raise WechatIlinkError("BAD_RESPONSE", "confirmed without bot_token")
            self.bot_token = token
            self.ilink_bot_id = data.get("ilink_bot_id") if isinstance(data.get("ilink_bot_id"), str) else None
            self.ilink_user_id = data.get("ilink_user_id") if isinstance(data.get("ilink_user_id"), str) else None
        return {"status": status}

    def _require_session(self) -> None:
        if not self.bot_token:
            raise WechatIlinkError("NO_SESSION", "no confirmed iLink session")

    async def fetch_messages(self) -> list[dict[str, Any]]:
        """One long-poll round. Returns raw message items (caller normalizes)."""

        self._require_session()
        data = await self._post("/api/messages/sync", {}, with_token=True)
        items = data.get("messages", [])
        if not isinstance(items, list):
            raise WechatIlinkError("BAD_RESPONSE", "messages is not a list")
        return [m for m in items if isinstance(m, dict)]

    async def send_text(self, to: str, text: str) -> dict[str, Any]:
        """Send one text message through the confirmed session."""

        self._require_session()
        if not to or not text:
            raise WechatIlinkError("BAD_REQUEST", "send needs a target and text")
        return await self._post(
            "/api/messages/send", {"to": to, "text": text}, with_token=True
        )


__all__ = [
    "ILINK_BASE_URL",
    "QR_STATUSES",
    "WechatIlinkClient",
    "WechatIlinkError",
    "validate_base_url",
    "validate_weixin_https_url",
]
