"""Usage board: read-only local commands, cached snapshot, stale flag."""

from __future__ import annotations

import json
import sys
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server.bot_team.usage import (
    read_usage,
    refresh_usage,
    usage_commands_from_env,
    usage_snapshot,
)
from server.routes.bot_team import router as bots_router

USER = {"user_id": "usage-user", "username": "usage"}

ECHO = [sys.executable, "-c", "import json; print(json.dumps({'used': 12, 'limit': 100, 'unit': 'credits'}))"]


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("VEYA_BOTS_ROOT", str(tmp_path / "bots-root"))
    app = FastAPI()
    from server import auth as _w11_auth

    app.dependency_overrides[_w11_auth.require_user] = lambda: dict(USER)
    app.include_router(bots_router)
    return TestClient(app)


def test_commands_from_env_parsing(monkeypatch):
    monkeypatch.setenv("VEYA_USAGE_COMMANDS", json.dumps({"claude": ECHO}))
    assert usage_commands_from_env() == {"claude": ECHO}
    monkeypatch.setenv("VEYA_USAGE_COMMANDS", "{not json")
    assert usage_commands_from_env() == {}
    monkeypatch.setenv("VEYA_USAGE_COMMANDS", json.dumps({"bad name!": ECHO, "ok": "nope"}))
    assert usage_commands_from_env() == {}
    monkeypatch.delenv("VEYA_USAGE_COMMANDS", raising=False)
    assert usage_commands_from_env() == {}


def test_read_usage_parses_numbers_and_errors():
    snapshot = read_usage(
        {
            "good": ECHO,
            "bad-output": [sys.executable, "-c", "print('hello')"],
            "failing": [sys.executable, "-c", "raise SystemExit(3)"],
            "missing": ["/nonexistent/binary-xyz"],
        }
    )
    assert snapshot["configured"] is True
    assert snapshot["providers"]["good"] == {
        "used": 12,
        "limit": 100,
        "unit": "credits",
        "remaining": 88,
    }
    assert "error" in snapshot["providers"]["bad-output"]
    assert "error" in snapshot["providers"]["failing"]
    assert "error" in snapshot["providers"]["missing"]


def test_snapshot_cache_and_stale(tmp_path):
    assert usage_snapshot(root=tmp_path)["providers"] == {}
    fresh = refresh_usage({"good": ECHO}, root=tmp_path)
    assert usage_snapshot(root=tmp_path)["providers"]["good"]["used"] == 12
    assert usage_snapshot(root=tmp_path)["stale"] is False
    stale = dict(fresh)
    stale["updated_ts"] = time.time() - 3600
    (tmp_path / "usage.json").write_text(json.dumps(stale), encoding="utf-8")
    assert usage_snapshot(root=tmp_path)["stale"] is True


def test_usage_endpoints_and_board(monkeypatch, client):
    monkeypatch.setenv("VEYA_USAGE_COMMANDS", json.dumps({"good": ECHO}))

    unconfigured = client.get("/api/v1/bots/usage")
    assert unconfigured.status_code == 200
    assert unconfigured.json()["providers"] == {}

    refreshed = client.post("/api/v1/bots/usage/refresh")
    assert refreshed.status_code == 200, refreshed.text
    assert refreshed.json()["providers"]["good"]["used"] == 12

    cached = client.get("/api/v1/bots/usage")
    assert cached.json()["providers"]["good"]["limit"] == 100

    board = client.get("/api/v1/bots/board")
    assert board.status_code == 200
    assert board.json()["usage"]["providers"]["good"]["used"] == 12
