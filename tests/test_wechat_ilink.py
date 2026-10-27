"""WeChat iLink: QR validation, status handling, adapter parse/send."""

from __future__ import annotations

import pytest

from server.channels import adapter_for
from server.channels.clients import ChannelNotConfigured, client_for, reset_clients
from server.channels.wechat import WechatAdapter, WechatPollerClient
from veya.obase.channels.wechat_ilink import (
    WechatIlinkClient,
    WechatIlinkError,
    validate_base_url,
    validate_weixin_https_url,
)


class FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class FakeHttp:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def post(self, url, headers=None, json=None):
        self.calls.append((url, json))
        return FakeResp(self.responses.pop(0))

    async def aclose(self):
        pass


def test_qr_url_validation():
    assert validate_weixin_https_url("https://weixin.qq.com/q/abc") == "https://weixin.qq.com/q/abc"
    assert validate_weixin_https_url("https://x.weixin.qq.com/q") == "https://x.weixin.qq.com/q"
    for bad in (
        "http://weixin.qq.com/q",
        "https://user:pass@weixin.qq.com/q",
        "https://evil.com/q",
        "https://weixin.qq.com.evil.com/q",
        "",
    ):
        with pytest.raises(WechatIlinkError):
            validate_weixin_https_url(bad)
    with pytest.raises(WechatIlinkError):
        validate_base_url("https://evil.com")


@pytest.mark.asyncio
async def test_qr_flow_and_send():
    http = FakeHttp(
        [
            {"qrcode_img_content": "https://weixin.qq.com/q/abc", "qrcode": "QR1"},
            {"status": "wait"},
            {"status": "confirmed", "bot_token": "tok", "ilink_bot_id": "b1", "ilink_user_id": "u1"},
            {"ok": True},
        ]
    )
    client = WechatIlinkClient(http_client=http)
    challenge = await client.begin_qr()
    assert challenge["qr_url"] == "https://weixin.qq.com/q/abc"
    assert await client.poll_qr("QR1") == {"status": "wait"}
    assert await client.poll_qr("QR1") == {"status": "confirmed"}
    assert client.bot_token == "tok"
    out = await client.send_text("conv1", "hi")
    assert out == {"ok": True}
    assert http.calls[-1][1] == {"to": "conv1", "text": "hi"}


@pytest.mark.asyncio
async def test_no_session_refuses():
    client = WechatIlinkClient(http_client=FakeHttp([]))
    with pytest.raises(WechatIlinkError, match="NO_SESSION"):
        await client.fetch_messages()
    with pytest.raises(WechatIlinkError, match="NO_SESSION"):
        await client.send_text("c", "x")


def test_wechat_adapter_parse_and_send():
    adapter = adapter_for("wechat")
    assert isinstance(adapter, WechatAdapter)
    assert adapter.verify(payload={}, headers={}, query={}) is False
    msg = adapter.parse_inbound(
        payload={"type": "text", "from": "sender1", "conversation": "conv1", "text": "hello"},
        headers={},
    )
    assert msg is not None
    assert msg.platform == "wechat"
    assert msg.external_chat_id == "conv1"
    assert msg.external_user_id == "sender1"
    assert msg.text == "hello"
    assert adapter.parse_inbound(
        payload={"type": "image", "from": "s", "conversation": "c", "text": "x"}, headers={}
    ) is None
    assert adapter.parse_inbound(payload={"type": "text", "from": "s"}, headers={}) is None


@pytest.mark.asyncio
async def test_wechat_adapter_send_chunks():
    class FakeClient:
        def __init__(self):
            self.sent = []

        async def send_text(self, chat_id, text):
            self.sent.append((chat_id, text))
            return {"ok": True}

    adapter = WechatAdapter()
    client = FakeClient()
    out = await adapter.send_reply(client, external_chat_id="c1", text="y" * 9000)
    assert out["ok"] is True
    assert out["chunks"] == len(client.sent) == 3
    assert "".join(t for _, t in client.sent) == "y" * 9000


@pytest.mark.asyncio
async def test_poller_client_requires_secret(monkeypatch):
    reset_clients()
    monkeypatch.delenv("WECHAT_EDGE_SECRET", raising=False)
    with pytest.raises(ChannelNotConfigured):
        await client_for("wechat")
    monkeypatch.setenv("WECHAT_EDGE_SECRET", "edge-secret")
    monkeypatch.setenv("WECHAT_POLLER_URL", "http://127.0.0.1:19451/reply")
    client = await client_for("wechat")
    assert isinstance(client, WechatPollerClient)
    assert client.poller_url == "http://127.0.0.1:19451/reply"
    reset_clients()
