"""Builtin routine templates: validate, list, instantiate into a bot's registry."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server import auth as _routines_auth
from server.goal_run.bot_identity import BotRegistry, BotSpec
from server.goal_run.routine_registry import validate_routine_spec
from server.goal_run.routine_templates import (
    ROUTINE_TEMPLATES,
    build_template_spec,
    get_template,
    list_templates,
)
from server.routes.routines import router as routines_router

USER = {"user_id": "template-user", "username": "template"}


def test_all_templates_validate():
    assert len(ROUTINE_TEMPLATES) == 6
    ids = [t["id"] for t in ROUTINE_TEMPLATES]
    assert len(set(ids)) == len(ids)
    for template in ROUTINE_TEMPLATES:
        spec = build_template_spec(template["id"], "alpha")
        assert validate_routine_spec(spec) == []
        assert spec.trigger_topic == "schedule.trigger"
        assert spec.goal_template == template["objective"]
        assert spec.bot_id == "alpha"


def test_unknown_template_raises():
    with pytest.raises(KeyError):
        get_template("nope")
    with pytest.raises(KeyError):
        build_template_spec("nope", "alpha")


def test_instantiation_mints_fresh_ids():
    a = build_template_spec("nightly-self-heal", "alpha")
    b = build_template_spec("nightly-self-heal", "alpha")
    assert a.routine_id != b.routine_id
    assert a.schedule == "0 2 * * *"


@pytest.fixture
def routine_env(tmp_path, monkeypatch):
    monkeypatch.setenv("VEYA_BOT_REGISTRY_PATH", str(tmp_path / "bot_registry.json"))
    monkeypatch.setenv("VEYA_ROUTINE_REGISTRY_PATH", str(tmp_path / "routine_registry.json"))
    registry = BotRegistry()
    registry.register(
        BotSpec(
            bot_id="alpha",
            owner_id=USER["user_id"],
            name="Alpha",
            description="Does alpha work.",
            instructions="You only do alpha work.",
        )
    )
    app = FastAPI()
    app.dependency_overrides[_routines_auth.require_user] = lambda: dict(USER)
    app.include_router(routines_router)
    return TestClient(app)


def test_templates_api_list_and_instantiate(routine_env):
    listed = routine_env.get("/api/v1/bots/alpha/routines/templates")
    assert listed.status_code == 200, listed.text
    assert len(listed.json()["templates"]) == 6

    created = routine_env.post(
        "/api/v1/bots/alpha/routines/templates/nightly-self-heal/instantiate"
    )
    assert created.status_code == 200, created.text
    body = created.json()
    assert body["bot_id"] == "alpha"
    assert body["schedule"] == "0 2 * * *"
    assert body["routine_id"]

    unknown = routine_env.post("/api/v1/bots/alpha/routines/templates/nope/instantiate")
    assert unknown.status_code == 404

    routines = routine_env.get("/api/v1/bots/alpha/routines")
    assert routines.status_code == 200
    assert any(r["routine_id"] == body["routine_id"] for r in routines.json())
