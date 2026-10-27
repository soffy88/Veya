"""Slack channel: signature verification, event parsing, chunked replies."""

from __future__ import annotations

import hashlib
import hmac
import time

import pytest

from server.channels import adapter_for
from server.channels.base import SignatureRejected
from server.channels.slack import SlackAdapter, chunk_text_for_platform
from veya.obase.channels import PLATFORM_SLACK, verify_slack_signature

SECRET = "test-signing-secret"
BODY = b'{"type":"event_callback","event":{"type":"message","text":"hi","channel":"C1","user":"U1"}}'


def _sig(secret: str, ts: str, body: bytes) -> str:
    base = f"v0:{ts}:".encode("utf-8") + body
    return "v0=" + hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()


def test_verify_slack_signature_vectors():
    ts = str(int(time.time()))
    assert verify_slack_signature(
        signing_secret=SECRET, timestamp=ts, raw_body=BODY, provided=_sig(SECRET, ts, BODY)
    )
    assert not verify_slack_signature(
        signing_secret=SECRET, timestamp=ts, raw_body=b"tampered", provided=_sig(SECRET, ts, BODY)
    )
    stale = str(int(time.time()) - 600)
    assert not verify_slack_signature(
        signing_secret=SECRET, timestamp=stale, raw_body=BODY, provided=_sig(SECRET, stale, BODY)
    )
    assert not verify_slack_signature(
        signing_secret=None, timestamp=ts, raw_body=BODY, provided=_sig(SECRET, ts, BODY)
    )
    assert not verify_slack_signature(
        signing_secret=SECRET, timestamp="not-a-time", raw_body=BODY, provided="v0=x"
    )


def test_slack_adapter_resolves_and_verifies():
    adapter = adapter_for("slack")
    assert isinstance(adapter, SlackAdapter)
    assert adapter.platform == PLATFORM_SLACK
    adapter.signing_secret = SECRET
    ts = str(int(time.time()))
    headers = {
        "x-slack-signature": _sig(SECRET, ts, BODY),
        "x-slack-request-timestamp": ts,
    }
    assert adapter.verify(payload={}, headers=headers, query={}, raw_body=BODY)
    assert not adapter.verify(payload={}, headers=headers, query={})
    with pytest.raises(SignatureRejected):
        adapter.check(payload={}, headers=headers, query={}, raw_body=b"tampered")


def test_slack_challenge_and_parse():
    adapter = SlackAdapter(signing_secret=SECRET, bot_token="xoxb-test")
    assert adapter.challenge_response({"type": "url_verification", "challenge": "abc"}) == {
        "challenge": "abc"
    }
    assert adapter.challenge_response({"type": "event_callback"}) is None

    import json

    msg = adapter.parse_inbound(payload=json.loads(BODY), headers={})
    assert msg is not None
    assert msg.platform == "slack"
    assert msg.external_chat_id == "C1"
    assert msg.external_user_id == "U1"
    assert msg.text == "hi"

    bot_msg = adapter.parse_inbound(
        payload={
            "type": "event_callback",
            "event": {"type": "message", "subtype": "bot_message", "text": "x", "channel": "C1", "user": "U1"},
        },
        headers={},
    )
    assert bot_msg is None
    assert adapter.parse_inbound(payload={"type": "event_callback", "event": {"type": "reaction_added"}}, headers={}) is None


@pytest.mark.asyncio
async def test_slack_send_reply_chunks():
    class FakeClient:
        def __init__(self):
            self.sent = []

        async def post_message(self, channel, text):
            self.sent.append((channel, text))
            return {"ok": True}

    adapter = SlackAdapter(signing_secret=SECRET, bot_token="xoxb-test")
    client = FakeClient()
    long_text = "x" * 9000
    out = await adapter.send_reply(client, external_chat_id="C9", text=long_text)
    assert out["ok"] is True
    assert out["chunks"] == len(client.sent) == 3
    assert "".join(t for _, t in client.sent) == long_text
    assert chunk_text_for_platform("ab") == ["ab"]
