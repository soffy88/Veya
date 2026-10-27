"""Companions: presets bundle persona + routine work into one bot."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server import auth as _companions_auth
from server.bot_team.companions import (
    COMPANION_PRESETS,
    create_companion,
    list_presets,
)
from server.goal_run.bot_identity import BotRegistry
from server.goal_run.routine_registry import RoutineRegistry
from server.goal_run.routine_templates import ROUTINE_TEMPLATES
from server.routes.bot_team import router as bots_router

USER = {"user_id": "companion-user", "username": "companion"}


def test_presets_reference_real_templates():
    template_ids = {t["id"] for t in ROUTINE_TEMPLATES}
    assert len(COMPANION_PRESETS) == 3
    for preset in COMPANION_PRESETS:
        assert preset["routine_templates"]
        assert set(preset["routine_templates"]) <= template_ids
    assert [p["id"] for p in list_presets()] == [p["id"] for p in COMPANION_PRESETS]


def test_create_companion_registers_bot_and_routines(tmp_path, monkeypatch):
    monkeypatch.setenv("VEYA_BOT_REGISTRY_PATH", str(tmp_path / "bot_registry.json"))
    monkeypatch.setenv("VEYA_ROUTINE_REGISTRY_PATH", str(tmp_path / "routine_registry.json"))
    registry = BotRegistry()
    routines = RoutineRegistry(bot_id="res1")
    spec, routine_ids = create_companion(
        registry, routines, owner_id=USER["user_id"], preset_id="researcher", bot_id="res1"
    )
    assert spec.kind == "companion"
    assert spec.personality
    assert spec.instructions
    assert len(routine_ids) == 2
    assert registry.get("res1") is not None
    assert {r.routine_id for r in RoutineRegistry(bot_id="res1").list()} == set(routine_ids)
    with pytest.raises(ValueError, match="already exists"):
        create_companion(
            registry, routines, owner_id=USER["user_id"], preset_id="researcher", bot_id="res1"
        )


@pytest.fixture
def companion_client(tmp_path, monkeypatch):
    monkeypatch.setenv("VEYA_BOT_REGISTRY_PATH", str(tmp_path / "bot_registry.json"))
    monkeypatch.setenv("VEYA_ROUTINE_REGISTRY_PATH", str(tmp_path / "routine_registry.json"))
    app = FastAPI()
    app.dependency_overrides[_companions_auth.require_user] = lambda: dict(USER)
    app.include_router(bots_router)
    return TestClient(app)


def test_companions_api(companion_client):
    presets = companion_client.get("/api/v1/bots/companions/presets")
    assert presets.status_code == 200, presets.text
    assert len(presets.json()["presets"]) == 3

    created = companion_client.post(
        "/api/v1/bots/companions",
        json={"preset_id": "secretary", "bot_id": "sec1"},
    )
    assert created.status_code == 200, created.text
    body = created.json()
    assert body["bot"]["kind"] == "companion"
    assert len(body["routines"]) == 2

    duplicate = companion_client.post(
        "/api/v1/bots/companions",
        json={"preset_id": "secretary", "bot_id": "sec1"},
    )
    assert duplicate.status_code == 409

    unknown = companion_client.post(
        "/api/v1/bots/companions",
        json={"preset_id": "nope", "bot_id": "sec2"},
    )
    assert unknown.status_code == 404
