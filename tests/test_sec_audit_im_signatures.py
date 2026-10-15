import hashlib
from veya.oskill.im.wechat import WeChatGateway
from veya.oskill.im.discord import DiscordGateway


def test_wechat_signatures_are_required_and_constant_time_compatible():
    gateway = WeChatGateway(app_id="", app_secret="", token="test-only-token", runner=None)
    assert gateway.verify_signature("", "1", "2") is False
    expected = hashlib.sha1("".join(sorted(["test-only-token", "1", "2"])).encode()).hexdigest()
    assert gateway.verify_signature(expected, "1", "2") is True
    assert gateway.verify_signature("wrong", "1", "2") is False
    unconfigured = WeChatGateway(app_id="", app_secret="", token="", runner=None)
    assert unconfigured.verify_signature(expected, "1", "2") is False


def test_discord_signatures_fail_closed_without_key_or_headers():
    gateway = DiscordGateway(public_key="", runner=None)
    import asyncio
    assert asyncio.run(gateway.verify_signature(b"{}", "signature", "timestamp")) is False
    configured = DiscordGateway(public_key="not-a-valid-ed25519-key", runner=None)
    assert asyncio.run(configured.verify_signature(b"{}", "bad", "timestamp")) is False
