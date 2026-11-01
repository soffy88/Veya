"""Runner protocol version handshake: old peers keep working, new requirements 426."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server.bot_team.runner_client import (
    _MIN_RUNNER_VERSION,
    _TURN_PAYLOAD_VERSION,
    build_turn_payload,
    check_runner_protocol,
)
from server.goal_run.bot_identity import BotSpec
from server.routes.runners import RunnerTurnRequest, router as runners_router

BOT = {
    "bot_id": "chef",
    "name": "Chef",
    "description": "Cooks.",
    "instructions": "Cook.",
    "provider": "",
    "model": "",
    "owner_id": "owner-1",
}


def test_payload_carries_versions():
    payload = build_turn_payload(
        bot=BotSpec(bot_id="chef", owner_id="owner-1", name="Chef"),
        text="hi",
        user={"user_id": "u1"},
    )
    assert payload["version"] == _TURN_PAYLOAD_VERSION == 1
    assert payload["min_runner_version"] == _MIN_RUNNER_VERSION == 1


def test_check_runner_protocol():
    check_runner_protocol(1)
    check_runner_protocol(0)
    with pytest.raises(ValueError, match="requires runner protocol"):
        check_runner_protocol(2)
    with pytest.raises(ValueError, match="speaks 1"):
        check_runner_protocol(2, runner_version=1)
    check_runner_protocol(2, runner_version=2)


def test_request_model_defaults_old_hub():
    req = RunnerTurnRequest(bot=BOT, text="hi", user={"user_id": "u1"})
    assert req.version == 1
    assert req.min_runner_version == 1
    req2 = RunnerTurnRequest(bot=BOT, text="hi", user={"user_id": "u1"}, min_runner_version=3)
    assert req2.min_runner_version == 3


@pytest.fixture
def runner_client(monkeypatch):
    async def fake_turn(**kwargs: Any):
        return SimpleNamespace(to_dict=lambda: {"reply": "ok", "status": "completed"})

    monkeypatch.setattr("server.bot_team.turns.run_bot_turn", fake_turn)

    class _FakeAuth:
        def verify(self, authorization: str):
            return SimpleNamespace(permissions=SimpleNamespace(write=True))

    monkeypatch.setattr("server.routes.runners.runner_auth", lambda: _FakeAuth())
    app = FastAPI()
    app.include_router(runners_router)
    return TestClient(app)


def test_turn_endpoint_accepts_current_and_426s_newer(runner_client):
    ok = runner_client.post(
        "/api/v1/runners/turn",
        json={"bot": BOT, "text": "hi", "user": {"user_id": "u1"}, "min_runner_version": 1},
        headers={"Authorization": "Bearer x"},
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["turn"]["reply"] == "ok"

    old = runner_client.post(
        "/api/v1/runners/turn",
        json={"bot": BOT, "text": "hi", "user": {"user_id": "u1"}},
        headers={"Authorization": "Bearer x"},
    )
    assert old.status_code == 200, old.text

    new = runner_client.post(
        "/api/v1/runners/turn",
        json={"bot": BOT, "text": "hi", "user": {"user_id": "u1"}, "min_runner_version": 2},
        headers={"Authorization": "Bearer x"},
    )
    assert new.status_code == 426, new.text
    assert "runner protocol" in new.json()["detail"]


def test_auth_still_first(monkeypatch):
    from veya.remote.auth import RemoteAuthError

    class _DenyingAuth:
        def verify(self, authorization: str):
            raise RemoteAuthError("nope")

    monkeypatch.setattr("server.routes.runners.runner_auth", lambda: _DenyingAuth())
    app = FastAPI()
    app.include_router(runners_router)
    client = TestClient(app)
    resp = client.post(
        "/api/v1/runners/turn",
        json={"bot": BOT, "text": "hi", "user": {"user_id": "u1"}, "min_runner_version": 99},
        headers={"Authorization": "Bearer x"},
    )
    assert resp.status_code == 401
