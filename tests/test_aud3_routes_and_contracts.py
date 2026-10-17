"""AUD3 contract and route tests:
1. Session search and handoff/fork routes are mounted at /api/v1/sessions/* (no duplicate prefix).
2. _require_user in mcp_presets and role_skill_presets accepts incoming Bearer token and authenticates.
3. Webhook endpoints authenticate via HMAC signature without requiring interactive token.
4. Channel endpoints support GET methods correctly.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import pytest
from fastapi.testclient import TestClient

from server import auth as auth_mod
from server.app import app
from server.goal_run.bot_identity import BotRegistry, BotSpec
from server.goal_run.models import RoutineSpec
from server.goal_run.routine_registry import RoutineRegistry
from server.routes.session import _sessions


import uuid

@pytest.fixture
def auth_client(tmp_path, monkeypatch):
    username = f"aud3_{uuid.uuid4().hex[:8]}"
    user = auth_mod.create_user(username, "Password123!")
    token = auth_mod.issue_token(user["user_id"])
    c = TestClient(app)
    c.headers.update({"Authorization": f"Bearer {token}"})
    return c, user, token


def test_session_search_route_path_not_duplicated(auth_client):
    """GET /api/v1/sessions/search must not 404 due to duplicated /api/v1/sessions prefix."""
    client, user, token = auth_client
    resp = client.get("/api/v1/sessions/search?q=test")
    assert resp.status_code != 404, f"Unexpected 404: {resp.text}"
    assert resp.status_code == 200
    data = resp.json()
    assert "results" in data


def test_session_handoff_preview_route_path_not_duplicated(auth_client):
    """GET /api/v1/sessions/{id}/handoff/preview must not 404 due to duplicated prefix."""
    client, user, token = auth_client
    sid = "sess_aud3_test_1"
    _sessions[sid] = {
        "id": sid,
        "user_id": user["user_id"],
        "persona": "build",
        "project": "default",
        "config": {},
        "messages": [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi there"},
        ],
        "cost_usd": 0.0,
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
    }
    resp = client.get(f"/api/v1/sessions/{sid}/handoff/preview?mode=focused")
    assert resp.status_code != 404, f"Unexpected 404: {resp.text}"
    assert resp.status_code == 200
    data = resp.json()
    assert "brief" in data


def test_role_skill_presets_auth_dependency(auth_client, monkeypatch):
    """POST /api/v1/bots/{bot_id}/presets/apply must accept valid Bearer token, not 401."""
    monkeypatch.setenv("VEYA_ROLE_SKILL_PRESETS", "1")
    client, user, token = auth_client
    bot_id = "bot_presets_auth_test"
    BotRegistry().register(BotSpec(
        bot_id=bot_id,
        name="Preset Test Bot",
        owner_id=user["user_id"],
        runner_id="local",
    ))

    resp = client.post(
        f"/api/v1/bots/{bot_id}/presets/apply",
        json={"role_id": "researcher"},
    )
    # If _require_user ignores token, it returns 401 "请先登录"
    assert resp.status_code != 401, "Auth dependency _require_user rejected valid bearer token"
    assert resp.status_code in (200, 404, 422)  # May 404 if role not found, but NOT 401


def test_mcp_presets_auth_dependency(auth_client, monkeypatch):
    """POST /api/v1/mcp-presets/{name}/attach must accept valid Bearer token, not 401."""
    monkeypatch.setenv("VEYA_MCP_PRESETS", "1")
    client, user, token = auth_client
    bot_id = "bot_mcp_auth_test"
    BotRegistry().register(BotSpec(
        bot_id=bot_id,
        name="MCP Test Bot",
        owner_id=user["user_id"],
        runner_id="local",
    ))

    resp = client.post(
        "/api/v1/mcp-presets/markitdown/attach",
        json={"bot_id": bot_id},
    )
    assert resp.status_code != 401, "Auth dependency _require_user rejected valid bearer token"


def test_routine_webhook_signature_auth_without_user_token(monkeypatch):
    """External webhook with valid HMAC signature must not be rejected by AuthGateMiddleware."""
    monkeypatch.setenv("VEYA_EVENT_TRIGGERS", "1")
    monkeypatch.setenv("VEYA_ROUTINE_TRIGGERS", "1")
    secret = "w" * 32
    monkeypatch.setenv("VEYA_WEBHOOK_SECRET", secret)

    bot_id = f"bot_webhook_{uuid.uuid4().hex[:6]}"
    BotRegistry().register(BotSpec(
        bot_id=bot_id,
        name="Webhook Bot",
        owner_id="webhook_user",
        runner_id="local",
    ))
    RoutineRegistry(bot_id=bot_id).register(RoutineSpec(
        routine_id="r-wh-1",
        trigger_topic="event.arrive",
        objective="Webhook trigger",
        goal_template="Webhook trigger",
        enabled=True,
        bot_id=bot_id,
        triggers=[{"kind": "webhook", "event": "release"}],
    ))

    body = json.dumps({"event": "release", "id": "rel_999"}).encode("utf-8")
    sig = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()

    # Unauthenticated client (NO Authorization header)
    anon_client = TestClient(app)
    resp = anon_client.post(
        f"/api/v1/bots/{bot_id}/routines/webhook",
        data=body,
        headers={"X-Veya-Signature": sig, "Content-Type": "application/json"},
    )
    # Must not be blocked by AuthGateMiddleware with {"detail": "请先登录"}
    assert resp.status_code == 200, f"Webhook rejected: {resp.status_code} {resp.text}"
    assert resp.json()["status"] == "ok"



def test_handoff_preview_works_for_durable_session(auth_client):
    """A session created through the unified (durable) API must be usable by
    handoff/fork.  Before the fix, ``_owned_session`` only consulted the
    in-memory ``_sessions`` dict, so every durable session answered 404 even
    though the frontend created it moments earlier."""
    client, user, token = auth_client

    created = client.post("/api/v1/sessions", json={})
    assert created.status_code == 200, created.text
    sid = created.json()["session_id"]

    preview = client.get(f"/api/v1/sessions/{sid}/handoff/preview?mode=focused")
    assert preview.status_code == 200, f"durable session not found by handoff: {preview.text}"
    assert "brief" in preview.json()

    fork = client.get(f"/api/v1/sessions/{sid}/fork/preview")
    assert fork.status_code == 200, f"durable session not found by fork: {fork.text}"


def test_handoff_still_hides_other_users_durable_session(auth_client):
    """Ownership must not regress: another user's durable session stays 404."""
    client, user, token = auth_client

    other = auth_mod.create_user(f"aud3_other_{uuid.uuid4().hex[:8]}", "Password123!")
    other_token = auth_mod.issue_token(other["user_id"])
    other_client = TestClient(app)
    other_client.headers.update({"Authorization": f"Bearer {other_token}"})

    created = other_client.post("/api/v1/sessions", json={})
    assert created.status_code == 200, created.text
    sid = created.json()["session_id"]

    resp = client.get(f"/api/v1/sessions/{sid}/handoff/preview?mode=focused")
    assert resp.status_code == 404, "must not leak another user's session"
