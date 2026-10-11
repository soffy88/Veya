"""NANO-W7: chat-app channels + morning feed.

Covers the parts that are easy to get wrong:

* every adapter rejects an unverified or unconfigured webhook (fail closed)
* inbound payloads normalise into a ChannelMessage; non-messages are ignored
* long replies are chunked to the platform limit
* the binding table persists, is keyed by (platform, chat), and is owner-scoped
* a linking code binds exactly once and is never executed as a message
* the feature is invisible (404) while the env flags are off
* daily_brief honours its omodul contract, and the delivered artifact exists
* external HTTP is mocked everywhere; no test touches the network
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server.channels import (
    ChannelBindingStore,
    FeishuAdapter,
    LinkRequest,
    SignatureRejected,
    TelegramAdapter,
    adapter_for,
    binding_store,
    issue_code,
)
from server.channels.dingtalk import DingTalkAdapter
from server.channels.dispatch import anonymous_ref, dispatch_inbound
from server.channels.linking import CodeStore
from server.channels.wecom import WeComAdapter
from server.routes import channels as channel_routes
from veya.obase.channels import PLATFORMS, chunk_text, verify_feishu_signature

# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


@pytest.fixture
def channels_env(tmp_path, monkeypatch):
    """All channel state lands in tmp_path; nothing touches the real ~/.veya."""

    monkeypatch.setenv("VEYA_CHANNELS_ROOT", str(tmp_path / "channels"))
    monkeypatch.setenv("VEYA_MORNING_FEED_ROOT", str(tmp_path / "morning_feed"))
    monkeypatch.setenv("VEYA_CHANNELS", "1")
    monkeypatch.setenv("VEYA_MORNING_FEED", "1")
    # Secrets come from env only.
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tg-token")
    monkeypatch.setenv("TELEGRAM_SECRET_TOKEN", "tg-secret")
    monkeypatch.setenv("FEISHU_APP_ID", "fs-app")
    monkeypatch.setenv("FEISHU_APP_SECRET", "fs-secret")
    monkeypatch.setenv("FEISHU_ENCRYPT_KEY", "fs-encrypt")
    monkeypatch.setenv("DINGTALK_WEBHOOK_SECRET", "dt-secret")
    monkeypatch.setenv("WECOM_TOKEN", "wc-token")

    channel_routes.reset_binding_store()
    from server.channels import clients as client_module

    client_module.reset_clients()
    yield
    channel_routes.reset_binding_store()
    client_module.reset_clients()


@pytest.fixture
def client(channels_env) -> TestClient:
    """TestClient over the channels router only (server.app is not importable)."""

    app = FastAPI()
    app.include_router(channel_routes.router)
    return TestClient(app)


class _StubClient:
    """Records outbound sends instead of performing HTTP."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def send_message(self, chat_id: str, text: str, **_kwargs: Any) -> dict[str, Any]:
        self.sent.append((str(chat_id), str(text)))
        return {"ok": True}


def _telegram_update(text: str = "hello", chat_id: int = 42) -> dict[str, Any]:
    return {
        "update_id": 1,
        "message": {
            "message_id": 1,
            "text": text,
            "chat": {"id": chat_id, "type": "private"},
            "from": {"id": 9001, "is_bot": False},
        },
    }


# --------------------------------------------------------------------------
# Signature verification: fail closed
# --------------------------------------------------------------------------


def test_telegram_rejects_missing_or_wrong_secret(channels_env):
    adapter = adapter_for("telegram")
    good = {"x-telegram-bot-api-secret-token": "tg-secret"}

    assert adapter.verify(payload=_telegram_update(), headers=good, query={}) is True
    # wrong secret
    assert (
        adapter.verify(payload={}, headers={"x-telegram-bot-api-secret-token": "nope"}, query={})
        is False
    )
    # header absent entirely
    assert adapter.verify(payload={}, headers={}, query={}) is False

    with pytest.raises(SignatureRejected):
        adapter.check(payload={}, headers={}, query={})


def test_telegram_without_configured_secret_fails_closed(monkeypatch):
    """An unconfigured secret must not authenticate anything."""
    monkeypatch.delenv("TELEGRAM_SECRET_TOKEN", raising=False)
    adapter = TelegramAdapter(secret_token=None)
    assert (
        adapter.verify(payload={}, headers={"x-telegram-bot-api-secret-token": "x"}, query={})
        is False
    )


def test_dingtalk_rejects_bad_or_stale_signature(channels_env):
    import time as _time

    adapter = adapter_for("dingtalk")
    timestamp = str(int(_time.time() * 1000))  # ms epoch, fresh

    def payload(ts: str, sign: str) -> dict[str, Any]:
        return {"timestamp": ts, "sign": sign}

    def sign_of(ts: str) -> str:
        return base64.b64encode(
            hmac.new(b"dt-secret", f"{ts}\ndt-secret".encode(), hashlib.sha256).digest()
        ).decode()

    assert (
        adapter.verify(payload=payload(timestamp, sign_of(timestamp)), headers={}, query={}) is True
    )
    # tampered signature
    assert adapter.verify(payload=payload(timestamp, "AAAA"), headers={}, query={}) is False
    # stale timestamp (replay guard) — correctly signed, but hours old
    stale = str(int(_time.time() * 1000) - 7_200_000)
    assert adapter.verify(payload=payload(stale, sign_of(stale)), headers={}, query={}) is False


def test_feishu_signature_matches_documented_scheme(channels_env):
    timestamp, nonce, key = "1700000000", "abc", "fs-encrypt"
    expected = hmac.new(
        key.encode(), f"{timestamp}{nonce}{key}".encode(), hashlib.sha256
    ).hexdigest()
    assert (
        verify_feishu_signature(
            encrypt_key=key, timestamp=timestamp, nonce=nonce, signature=expected
        )
        is True
    )
    assert (
        verify_feishu_signature(
            encrypt_key=key, timestamp=timestamp, nonce=nonce, signature="deadbeef"
        )
        is False
    )
    adapter = adapter_for("feishu")
    assert (
        adapter.verify(
            payload={},
            headers={
                "x-lark-signature": expected,
                "x-lark-request-timestamp": timestamp,
                "x-lark-request-nonce": nonce,
            },
            query={},
        )
        is True
    )


def test_wecom_signature_and_url_handshake(channels_env):
    adapter = adapter_for("wecom")
    token, timestamp, nonce, encrypt = "wc-token", "1700000000", "n1", "CIPHER"
    sig = hashlib.sha1("".join(sorted([token, timestamp, nonce, encrypt])).encode()).hexdigest()
    query = {"msg_signature": sig, "timestamp": timestamp, "nonce": nonce}
    assert adapter.verify(payload={"Encrypt": encrypt}, headers={}, query=query) is True
    assert adapter.verify(payload={"Encrypt": encrypt}, headers={}, query={}) is False

    url_sig = hashlib.sha1("".join(sorted([token, timestamp, nonce])).encode()).hexdigest()
    assert adapter.verify_url(signature=url_sig, timestamp=timestamp, nonce=nonce) is True
    assert adapter.verify_url(signature="bad", timestamp=timestamp, nonce=nonce) is False


def test_all_platforms_are_registered():
    assert set(adapter_for(p).platform for p in PLATFORMS) == set(PLATFORMS)


# --------------------------------------------------------------------------
# Inbound parsing
# --------------------------------------------------------------------------


def test_parse_inbound_telegram_and_ignore_non_message(channels_env):
    adapter = adapter_for("telegram")
    message = adapter.parse_inbound(payload=_telegram_update("hi"), headers={})
    assert message is not None
    assert message.external_chat_id == "42"
    assert message.external_user_id == "9001"
    assert message.text == "hi"

    # edited-less update with no text, and a bare update_id, are both ignored
    assert adapter.parse_inbound(payload={"update_id": 3}, headers={}) is None
    assert (
        adapter.parse_inbound(
            payload={"message": {"chat": {"id": 1}, "from": {"id": 2}}}, headers={}
        )
        is None
    )


def test_parse_inbound_feishu_decodes_json_content(channels_env):
    adapter = FeishuAdapter()
    payload = {
        "event": {
            "message": {"content": json.dumps({"text": "你好"}), "chat_id": "oc_1"},
            "sender": {"sender_id": {"open_id": "ou_9"}},
        }
    }
    message = adapter.parse_inbound(payload=payload, headers={})
    assert message is not None
    assert message.external_chat_id == "oc_1"
    assert message.external_user_id == "ou_9"
    assert message.text == "你好"


def test_feishu_url_verification_challenge(channels_env):
    adapter = FeishuAdapter()
    assert adapter.challenge_response({"type": "url_verification", "challenge": "c1"}) == {
        "challenge": "c1"
    }
    assert adapter.challenge_response({"event": {}}) is None


def test_parse_inbound_dingtalk_and_wecom(channels_env):
    dt = DingTalkAdapter()
    message = dt.parse_inbound(
        payload={"text": {"content": "ping"}, "conversationId": "cid-1", "senderStaffId": "s1"},
        headers={},
    )
    assert message is not None and message.external_chat_id == "cid-1"

    wc = WeComAdapter()
    xml = (
        "<xml><FromUserName>u1</FromUserName><MsgType>text</MsgType>"
        "<Content>hello wecom</Content></xml>"
    )
    parsed = wc.parse_inbound(payload={"xml": xml}, headers={})
    assert parsed is not None
    assert parsed.external_user_id == "u1"
    assert parsed.text == "hello wecom"
    # malformed XML is ignored rather than raising
    assert wc.parse_inbound(payload={"xml": "<xml>"}, headers={}) is None


# --------------------------------------------------------------------------
# Reply chunking
# --------------------------------------------------------------------------


def test_long_replies_are_chunked_per_platform(channels_env):
    stub = _StubClient()
    adapter = adapter_for("telegram")
    text = "x" * 9000  # Telegram limit is 4096

    import asyncio

    result = asyncio.run(adapter.send_reply(stub, external_chat_id="42", text=text))

    assert result["chunks"] == 3
    assert all(len(body) <= 4096 for _, body in stub.sent)
    assert "".join(body for _, body in stub.sent) == text


def test_short_reply_is_a_single_chunk(channels_env):
    stub = _StubClient()
    adapter = adapter_for("wecom")

    import asyncio

    result = asyncio.run(adapter.send_reply(stub, external_chat_id="u1", text="hi"))

    assert result["chunks"] == 1
    assert stub.sent == [("u1", "hi")]


def test_chunk_text_honours_explicit_max_len():
    chunks = chunk_text("abcdefghij", platform="telegram", max_len=4)
    assert chunks == ["abcd", "efgh", "ij"]


# --------------------------------------------------------------------------
# Binding table
# --------------------------------------------------------------------------


def test_bindings_persist_and_resolve_by_chat(tmp_path, channels_env):
    store = ChannelBindingStore(root=tmp_path / "store")
    store.bind(
        platform="telegram",
        external_chat_id="42",
        external_user_id="9001",
        owner_ref="u_owner",
        target_type="bot",
        target_id="chef",
    )

    # A fresh store instance sees the same record (it is on disk, not in RAM).
    reopened = ChannelBindingStore(root=tmp_path / "store")
    found = reopened.find_for_chat(platform="telegram", external_chat_id="42")
    assert found is not None
    assert found["target_id"] == "chef"
    assert found["owner_ref"] == "u_owner"

    # Wrong chat or wrong platform resolves to nothing.
    assert reopened.find_for_chat(platform="telegram", external_chat_id="99") is None
    assert reopened.find_for_chat(platform="feishu", external_chat_id="42") is None


def test_unbind_requires_owner(tmp_path, channels_env):
    store = ChannelBindingStore(root=tmp_path / "store")
    record = store.bind(
        platform="feishu",
        external_chat_id="oc_1",
        external_user_id="ou_1",
        owner_ref="u_owner",
        target_type="group",
        target_id="g1",
    )
    assert store.unbind(record["binding_id"], owner_ref="u_intruder") is False
    assert store.unbind(record["binding_id"], owner_ref="u_owner") is True
    assert store.get(record["binding_id"]) is None


def test_anonymous_ref_is_stable_and_non_pii(channels_env):
    ref = anonymous_ref("telegram", "9001")
    assert ref == anonymous_ref("telegram", "9001")
    assert ref != anonymous_ref("telegram", "9002")
    assert "9001" not in ref


# --------------------------------------------------------------------------
# Linking codes
# --------------------------------------------------------------------------


def test_link_code_is_single_use_and_expires():
    store = CodeStore(ttl_seconds=60)
    code = store.issue(payload={"owner": "u_1"})
    assert store.claim(code) == {"owner": "u_1"}
    # second claim fails: codes are single-use
    assert store.claim(code) is None

    expired = store.issue(payload={"owner": "u_2"})
    store._codes[expired]["expires_at"] = 0  # force expiry
    assert store.claim(expired) is None


# --------------------------------------------------------------------------
# HTTP surface
# --------------------------------------------------------------------------


def test_routes_are_hidden_when_flags_are_off(monkeypatch):
    monkeypatch.setenv("VEYA_CHANNELS", "0")
    monkeypatch.setenv("VEYA_MORNING_FEED", "0")
    app = FastAPI()
    app.include_router(channel_routes.router)
    http = TestClient(app)

    assert http.get("/api/v1/channels/status").status_code == 404
    assert http.get("/api/v1/channels/bindings").status_code == 404
    assert http.get("/api/v1/channels/morning-feed/config").status_code == 404
    assert http.post("/api/v1/channels/telegram/webhook", json={}).status_code == 404


def test_webhook_rejects_bad_signature(client):
    response = client.post(
        "/api/v1/channels/telegram/webhook",
        json=_telegram_update(),
        headers={"x-telegram-bot-api-secret-token": "wrong"},
    )
    assert response.status_code == 403


def test_webhook_ignores_unbound_chat_without_dispatch(client):
    response = client.post(
        "/api/v1/channels/telegram/webhook",
        json=_telegram_update("hello"),
        headers={"x-telegram-bot-api-secret-token": "tg-secret"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["status"] == "unbound"


def test_webhook_claim_code_binds_the_chat(client):
    issued = issue_code(
        LinkRequest(
            platform="telegram",
            target_type="bot",
            target_id="chef",
            owner_ref="u_owner",
        )
    )
    assert issued["code"]

    # The code is pasted into the external chat; the webhook consumes it.
    response = client.post(
        "/api/v1/channels/telegram/webhook",
        json=_telegram_update(issued["code"], chat_id=77),
        headers={"x-telegram-bot-api-secret-token": "tg-secret"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "bound"

    store: ChannelBindingStore = binding_store()
    bound = store.find_for_chat(platform="telegram", external_chat_id="77")
    assert bound is not None
    assert bound["target_id"] == "chef"
    assert bound["owner_ref"] == "u_owner"

    # Replaying the same code does nothing (and never reaches a bot).
    replay = client.post(
        "/api/v1/channels/telegram/webhook",
        json=_telegram_update(issued["code"], chat_id=77),
        headers={"x-telegram-bot-api-secret-token": "tg-secret"},
    )
    assert replay.json()["status"] != "bound"


def test_list_bindings_is_owner_scoped(client):
    store: ChannelBindingStore = binding_store()
    store.bind(
        platform="telegram",
        external_chat_id="1",
        external_user_id="1",
        owner_ref="u_a",
        target_type="bot",
        target_id="chef",
    )
    store.bind(
        platform="feishu",
        external_chat_id="2",
        external_user_id="2",
        owner_ref="u_b",
        target_type="bot",
        target_id="chef",
    )
    # get_current_user falls back to "anonymous", so no other owner's rows leak.
    rows = client.get("/api/v1/channels/bindings").json()["bindings"]
    assert rows == []


def test_feed_config_round_trip(client):
    saved = client.post(
        "/api/v1/channels/morning-feed/config",
        json={
            "enabled": True,
            "time": "07:30",
            "timezone": "Asia/Shanghai",
            "sections": ["routines", "rss"],
            "push_webpush": True,
            "push_channels": ["telegram"],
        },
    )
    assert saved.status_code == 200
    loaded = client.get("/api/v1/channels/morning-feed/config").json()
    assert loaded["time"] == "07:30"
    assert loaded["sections"] == ["routines", "rss"]
    assert loaded["push_channels"] == ["telegram"]


def test_feed_config_rejects_bad_time(client):
    response = client.post(
        "/api/v1/channels/morning-feed/config",
        json={"enabled": True, "time": "25:99"},
    )
    assert response.status_code == 422


# --------------------------------------------------------------------------
# Dispatch
# --------------------------------------------------------------------------


def test_dispatch_unbound_does_not_execute(channels_env):
    import asyncio

    outcome = asyncio.run(
        dispatch_inbound(
            adapter_for("telegram").parse_inbound(payload=_telegram_update(), headers={}),
            binding=None,
            adapter=adapter_for("telegram"),
        )
    )
    assert outcome.ok is True
    assert outcome.status == "unbound"
    assert outcome.reply == ""


def test_dispatch_routes_to_bot_and_replies(channels_env, monkeypatch, tmp_path):
    """A bound message reaches the bot turn path and the reply is posted back."""

    import asyncio

    from server.bot_team.turns import BotTurnResult
    from server.goal_run.bot_identity import BotRegistry, BotSpec

    # The dispatch resolves the bot through BotRegistry()'s env-configured path.
    monkeypatch.setenv("VEYA_BOT_REGISTRY_PATH", str(tmp_path / "bot_registry.json"))
    BotRegistry().register(BotSpec(bot_id="chef", name="Chef", owner_id="u_owner"))

    captured: dict[str, Any] = {}

    async def fake_run_bot_turn(*, bot: Any, text: str, user: dict[str, Any], **_kw: Any) -> Any:
        captured["text"] = text
        captured["user"] = dict(user)
        return BotTurnResult(
            bot_id="chef",
            task_id="t1",
            session_id="s1",
            reply="pong",
            status="completed",
            trace_id="tr1",
        )

    monkeypatch.setattr("server.bot_team.turns.run_bot_turn", fake_run_bot_turn)

    stub = _StubClient()
    monkeypatch.setattr("server.channels.clients.client_for", _async_return(stub))

    binding = {
        "owner_ref": "u_owner",
        "target_type": "bot",
        "target_id": "chef",
    }
    outcome = asyncio.run(
        dispatch_inbound(
            adapter_for("telegram").parse_inbound(payload=_telegram_update("ping"), headers={}),
            binding=binding,
            adapter=adapter_for("telegram"),
        )
    )

    assert outcome.status == "answered"
    assert captured["text"] == "ping"
    assert captured["user"]["user_id"] == "u_owner"
    assert stub.sent == [("42", "pong")]


def _async_return(value: Any):
    async def _inner(_platform: str) -> Any:
        return value

    return _inner


def test_dispatch_unknown_target_type_is_reported(channels_env):
    import asyncio

    outcome = asyncio.run(
        dispatch_inbound(
            adapter_for("telegram").parse_inbound(payload=_telegram_update(), headers={}),
            binding={"owner_ref": "u", "target_type": "wat", "target_id": "x"},
            adapter=adapter_for("telegram"),
        )
    )
    assert outcome.ok is False
    assert "unknown target_type" in outcome.detail


# --------------------------------------------------------------------------
# daily_brief omodul contract
# --------------------------------------------------------------------------


def test_daily_brief_contract_and_artifact(channels_env, tmp_path):
    import asyncio

    from veya.omodul.daily_brief import (
        DailyBriefConfig,
        DailyBriefInput,
        compute_fingerprint_for,
        daily_brief,
    )

    config = DailyBriefConfig(
        owner_ref="u_owner",
        timezone="Asia/Shanghai",
        sections=["routines", "goals"],
        push_channels=["telegram", "not-a-platform"],
    )
    data = DailyBriefInput(
        routines=[{"name": "晨跑", "enabled": True, "goal_template": "run 5k"}],
        goals=[{"title": "ship W7", "status": "completed"}],
    )
    result = asyncio.run(daily_brief(config, data, tmp_path / "out"))

    assert result["status"] == "completed"
    assert result["error"] is None
    assert result["cost_usd"] == 0.0
    assert Path(result["report_path"]).exists()
    markdown = result["findings"]["markdown"]
    assert "晨跑" in markdown
    assert "ship W7" in markdown
    # unconfigured sections are reported as empty
    assert "goalruns" in result["findings"]["empty"]
    # only known platforms get payloads
    assert list(result["findings"]["channel_payloads"]) == ["telegram"]
    # fingerprint is a pure function of config (SPEC 5.5 / C3)
    assert compute_fingerprint_for(config, data) == result["fingerprint"]


def test_daily_brief_pillars_and_no_pii(channels_env):
    from veya.omodul.daily_brief import DailyBriefConfig

    assert DailyBriefConfig._enabled_pillars == {"report", "cost"}
    # The fingerprint only ever sees the non-PII owner reference.
    assert "user_id" not in DailyBriefConfig._fingerprint_fields


def test_daily_brief_returns_failed_instead_of_raising(channels_env, tmp_path, monkeypatch):
    import asyncio
    import sys

    # veya.omodul re-exports the daily_brief *function*, which shadows the
    # module attribute, so reach the module through sys.modules.
    module = sys.modules["veya.omodul.daily_brief"]
    config = module.DailyBriefConfig(owner_ref="u_owner")
    assert config.owner_ref == "u_owner"

    def boom(*_a: Any, **_k: Any) -> str:
        raise RuntimeError("render exploded")

    monkeypatch.setattr(module, "_render", boom)
    result = asyncio.run(module.daily_brief(config, module.DailyBriefInput(), tmp_path / "out"))
    assert result["status"] == "failed"
    assert "render exploded" in result["error"]["message"]


def test_daily_brief_rejects_bad_section_names(channels_env, tmp_path):
    """Unknown section names are ignored, not rendered."""
    import asyncio

    from veya.omodul.daily_brief import DailyBriefConfig, DailyBriefInput, daily_brief

    config = DailyBriefConfig(owner_ref="u_owner", sections=["nope"])
    result = asyncio.run(daily_brief(config, DailyBriefInput(), tmp_path / "o2"))
    markdown = result["findings"]["markdown"]
    assert "nope" not in markdown
    assert "今日暂无更新" in markdown


def test_run_morning_feed_persists_and_delivers(channels_env, monkeypatch):
    from server.bot_team import morning_feed as feed_mod

    config = feed_mod.MorningFeedConfig(
        owner_ref="u_owner",
        push_webpush=False,
        sections=["routines"],
    )
    feed_mod.save_config(config)
    assert feed_mod.get_config("u_owner").sections == ["routines"]

    stub = _StubClient()
    monkeypatch.setattr(feed_mod, "_push_webpush", _async_return(True))
    monkeypatch.setattr(feed_mod, "_client", _async_return(stub))

    import asyncio

    result = asyncio.run(
        feed_mod.run_morning_feed(
            config,
            sections={
                "routines": [{"name": "晨跑", "enabled": True}],
                "goals": [],
                "goalruns": [],
                "memory_highlights": [],
                "rss_items": [],
            },
        )
    )
    assert result["status"] == "completed"
    assert Path(result["report_path"]).exists()


def test_routine_dispatch_routes_feed_and_leaves_the_rest(channels_env, monkeypatch):
    """build_routine_dispatch splits feed handoffs; others keep the default path."""
    from server.bot_team import morning_feed as feed_mod

    seen: dict[str, Any] = {}

    async def fake_default(handoffs: list[dict], **_kw: Any) -> None:
        seen["default"] = [h["routine_id"] for h in handoffs]

    async def fake_feed(handoffs: list[dict]) -> None:
        seen["feed"] = [h["routine_id"] for h in handoffs]

    monkeypatch.setattr("server.bot_team.routine_clock._default_handoff_consumer", fake_default)
    monkeypatch.setattr(feed_mod, "_dispatch_feed", fake_feed)

    import asyncio

    dispatch = feed_mod.build_routine_dispatch()
    asyncio.run(
        dispatch(
            [
                {"routine_id": "daily_brief_u1", "owner_ref": "u1"},
                {"routine_id": "other_routine"},
            ]
        )
    )
    assert seen["feed"] == ["daily_brief_u1"]
    assert seen["default"] == ["other_routine"]


def test_bind_code_refuses_a_bot_the_caller_does_not_own(monkeypatch):
    """Binding a chat to someone else's bot (or an unknown bot) is a 404."""
    import pytest
    from fastapi import HTTPException

    from server.goal_run import bot_identity
    from server.routes import channels as channels_routes

    class _Spec:
        owner_id = "someone-else"

    class _Registry:
        def get(self, bot_id):
            return _Spec() if bot_id == "theirs" else None

    monkeypatch.setattr(bot_identity, "BotRegistry", _Registry)
    with pytest.raises(HTTPException) as exc:
        channels_routes._require_owned_target("bot", "theirs", {"user_id": "me"})
    assert exc.value.status_code == 404
    with pytest.raises(HTTPException):
        channels_routes._require_owned_target("bot", "missing", {"user_id": "me"})
