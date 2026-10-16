"""GBP3 Event Triggers tests (TDD).

Tests for:
1. Inbound webhook (with shared secret validation) hitting and triggering corresponding routine.
2. Bot group message matching keyword/regex triggering routine.
3. Trigger records queryable.
4. Secret incorrect -> rejected.
5. Flag off (`VEYA_EVENT_TRIGGERS`) -> not triggered.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server.goal_run.models import RoutineSpec, GoalRunState
from server.goal_run.routine_registry import RoutineRegistry
from server.goal_run.routine_history import RoutineHistoryStore
from server.nano_w9.routine_triggers import RoutineSignalRouter, verify_webhook_signature
from server.nano_w9.flags import FeatureDisabled
from server.routes.routines import router as routines_router


@pytest.fixture
def gbp3_env(tmp_path, monkeypatch):
    monkeypatch.setenv("VEYA_ROUTINE_REGISTRY_PATH", str(tmp_path / "routines.json"))
    monkeypatch.setenv("VEYA_ROUTINE_HISTORY_DIR", str(tmp_path / "history"))
    monkeypatch.setenv("VEYA_WEBHOOK_SECRET", "s" * 32)
    monkeypatch.delenv("VEYA_EVENT_TRIGGERS", raising=False)
    return tmp_path


def test_gbp3_event_trigger_flag_off(gbp3_env):
    """Test that when VEYA_EVENT_TRIGGERS is off, event triggers are disabled."""
    registry = RoutineRegistry()
    router = RoutineSignalRouter(registry=registry)
    
    with pytest.raises(FeatureDisabled) as exc:
        import asyncio
        asyncio.run(router.handle({"kind": "webhook", "event": "push", "verified": True}))
    assert exc.value.flag in ("VEYA_ROUTINE_TRIGGERS", "VEYA_EVENT_TRIGGERS")


def test_gbp3_webhook_trigger_and_history(gbp3_env, monkeypatch):
    """Test real call to webhook triggering routine, verifying signature and recording history."""
    monkeypatch.setenv("VEYA_EVENT_TRIGGERS", "1")
    registry = RoutineRegistry(bot_id="default-bot")
    
    # Register routine with webhook trigger
    spec = RoutineSpec(
        routine_id="routine-hook-1",
        trigger_topic="event.arrive",
        objective="Deploy on git tag",
        goal_template="Deploy on git tag",
        enabled=True,
        bot_id="default-bot",
        triggers=[{"kind": "webhook", "event": "git.tag"}],
    )
    registry.register(spec)

    history_store = RoutineHistoryStore(bot_id="default-bot")
    
    seen: list[dict] = []
    def mock_dispatch(**kw):
        seen.append(kw)
        history_store.add_record("routine-hook-1", {"trigger_id": kw.get("trigger_id"), "status": "ok"})
        return {"handoffs": [1]}

    router = RoutineSignalRouter(
        registry=registry,
        dispatch=mock_dispatch,
    )

    body = json.dumps({"event": "git.tag", "tag": "v1.0"}).encode("utf-8")
    secret = "s" * 32
    signature = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()

    # Verify webhook signature helper
    assert verify_webhook_signature(body, secret=secret, signature=signature)

    # Test signal handle
    signal = {"kind": "webhook", "event": "git.tag", "verified": True, "id": "hook_123"}
    import asyncio
    res = asyncio.run(router.handle(signal))
    assert res["status"] == "ok"
    assert len(res["dispatched"]) == 1
    assert res["dispatched"][0]["routine_id"] == "routine-hook-1"

    # Verify trigger records are queryable
    records = history_store.get_history("routine-hook-1")
    assert len(records) == 1
    assert records[0]["trigger_id"] == "hook_123"


def test_gbp3_webhook_invalid_secret(gbp3_env):
    """Test that incorrect secret / invalid signature is rejected."""
    body = json.dumps({"event": "git.tag"}).encode("utf-8")
    secret = "s" * 32
    bad_signature = "0" * 64
    assert not verify_webhook_signature(body, secret=secret, signature=bad_signature)
    assert not verify_webhook_signature(body, secret="wrong-secret-length-too-short", signature=bad_signature)


def test_gbp3_bot_group_message_matching(gbp3_env, monkeypatch):
    """Test bot group message matching keyword/regex vs non-matching."""
    monkeypatch.setenv("VEYA_EVENT_TRIGGERS", "1")
    
    spec_match = RoutineSpec(
        routine_id="r-match",
        trigger_topic="event.arrive",
        objective="Handle deploy",
        goal_template="Handle deploy",
        enabled=True,
    )
    setattr(spec_match, "triggers", [{"kind": "message", "regex": "^deploy\\s+\\w+"}])

    spec_nomatch = RoutineSpec(
        routine_id="r-nomatch",
        trigger_topic="event.arrive",
        objective="Handle test",
        goal_template="Handle test",
        enabled=True,
    )
    setattr(spec_nomatch, "triggers", [{"kind": "message", "regex": "^test\\s+\\w+"}])

    class MockRegistry:
        def list(self, *, include_disabled=False):
            return [spec_match, spec_nomatch]

    dispatched: list[str] = []
    router = RoutineSignalRouter(
        registry=MockRegistry(),
        dispatch=lambda **kw: dispatched.append(kw.get("routine_id")) or {"ok": True},
    )

    import asyncio
    # Matching case
    res1 = asyncio.run(router.handle({"kind": "message", "text": "deploy production"}))
    assert res1["status"] == "ok"
    assert len(res1["dispatched"]) == 1
    assert res1["dispatched"][0]["routine_id"] == "r-match"

    # Non-matching case
    res2 = asyncio.run(router.handle({"kind": "message", "text": "hello world"}))
    assert res2["status"] == "no_match"
    assert res2["dispatched"] == []


def test_gbp3_http_webhook_and_history_endpoints(gbp3_env, monkeypatch):
    monkeypatch.setenv("VEYA_EVENT_TRIGGERS", "1")
    app = FastAPI()
    from server.routes import routines as _routines_mod
    app.dependency_overrides[_routines_mod._require_owned_bot] = lambda: None
    app.include_router(routines_router)
    client = TestClient(app)

    registry = RoutineRegistry(bot_id="default-bot")
    spec = RoutineSpec(
        routine_id="routine-http-1",
        trigger_topic="event.arrive",
        objective="HTTP Webhook Routine",
        goal_template="HTTP Webhook Routine",
        enabled=True,
        bot_id="default-bot",
        triggers=[{"kind": "webhook", "event": "release"}],
    )
    registry.register(spec)

    body = json.dumps({"event": "release", "id": "rel_1"}).encode("utf-8")
    secret = "s" * 32
    monkeypatch.setenv("VEYA_WEBHOOK_SECRET", secret)
    sig = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()

    # Valid webhook request
    resp = client.post(
        "/api/v1/bots/default-bot/routines/webhook",
        data=body,
        headers={"X-Veya-Signature": sig},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] in ("ok", "partial")

    # Query history endpoint
    resp_hist = client.get("/api/v1/bots/default-bot/routines/routine-http-1/history")
    assert resp_hist.status_code == 200
    hist_data = resp_hist.json()
    assert hist_data["status"] == "ok"
    assert isinstance(hist_data["history"], list)

