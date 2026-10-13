"""fixall 2026-10-14: real Web Push delivery must never wedge the event loop."""

from __future__ import annotations

import threading

from server.bot_team import push as push_mod
from server.bot_team.push import (
    PushNotifier,
    PushPayload,
    PushSubscription,
    SubscriptionStore,
    VapidKeyManager,
    default_sender,
)
from server.notification_center import WEB_PUSH_FLAG


def test_default_sender_bounds_the_push_service_request(monkeypatch):
    seen = {}

    def fake_webpush(**kwargs):
        seen.update(kwargs)

    monkeypatch.setattr(push_mod, "_webpush_module", lambda: fake_webpush)
    sub = PushSubscription.from_dict(
        {"endpoint": "https://push.example/x", "p256dh": "k", "auth": "a"}, user_id="u"
    )
    vapid = push_mod.VapidKeys(private_key="p", public_key="q")
    assert default_sender(PushPayload(title="t", body="b"), sub, vapid) == (True, "delivered")
    assert seen["timeout"] == push_mod._PUSH_SEND_TIMEOUT_S > 0


def test_observe_with_real_sender_does_not_block_the_caller(monkeypatch, tmp_path):
    release = threading.Event()
    done = threading.Event()
    calls = []

    def slow_send(self, *, user_id, payload):
        calls.append((user_id, payload.title))
        release.wait(5)
        done.set()
        return {}

    monkeypatch.setattr(PushNotifier, "send", slow_send)
    notifier = PushNotifier(store=SubscriptionStore(tmp_path / "s"), vapid=VapidKeyManager(tmp_path / "v"))
    assert notifier.sender is default_sender
    notifier.observe({"title": "hi", "content": "c", "user_id": "u1", "payload": {WEB_PUSH_FLAG: True}})
    # observe() returned while the send is still in flight
    assert not done.is_set()
    release.set()
    assert done.wait(5)
    assert calls == [("u1", "hi")]
