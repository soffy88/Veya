"""Routine holds: waiting on the owner, resolved by the attention queue."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server import auth as _w11_auth
from server.bot_team.routine_holds import RoutineHoldStore
from server.goal_run.bot_identity import BotRegistry, BotSpec
from server.goal_run.routine_registry import RoutineRegistry
from server.goal_run.routine_templates import build_template_spec
from server.routes.bot_team import router as bots_router
from server.routes.routines import router as routines_router

USER = {"user_id": "holds-user", "username": "holds"}


@pytest.fixture
def holds_env(tmp_path, monkeypatch):
    monkeypatch.setenv("VEYA_BOTS_ROOT", str(tmp_path / "bots-root"))
    monkeypatch.setenv("VEYA_BOT_REGISTRY_PATH", str(tmp_path / "bot_registry.json"))
    monkeypatch.setenv("VEYA_ROUTINE_REGISTRY_PATH", str(tmp_path / "routines.json"))
    monkeypatch.setenv("VEYA_ROUTINE_HISTORY_DIR", str(tmp_path / "history"))
    BotRegistry().register(
        BotSpec(bot_id="alpha", owner_id=USER["user_id"], name="Alpha")
    )
    spec = build_template_spec("nightly-self-heal", "alpha")
    RoutineRegistry(bot_id="alpha").register(spec)
    return {"routine_id": spec.routine_id}


@pytest.fixture
def client():
    app = FastAPI()
    app.dependency_overrides[_w11_auth.require_user] = lambda: dict(USER)
    app.dependency_overrides[_w11_auth.get_current_user] = lambda: dict(USER)
    app.include_router(bots_router)
    app.include_router(routines_router)
    return TestClient(app)


def _holds_url(routine_id: str) -> str:
    return f"/api/v1/bots/alpha/routines/{routine_id}/holds"


def test_hold_store_round_trip(tmp_path):
    store = RoutineHoldStore(root=tmp_path)
    hold = store.create(bot_id="alpha", routine_id="rt1", reason="needs a decision")
    assert hold["status"] == "held"
    assert store.list(bot_id="alpha", routine_id="rt1") == [hold]
    resolved = store.resolve(hold["id"], status="resumed", note="ok")
    assert resolved["status"] == "resumed"
    assert store.resolve(hold["id"], status="resumed") is None


def test_hold_linked_attention_resume(tmp_path):
    store = RoutineHoldStore(root=tmp_path)
    hold = store.create(
        bot_id="alpha", routine_id="rt1", reason="q", attention_id="att_1"
    )
    done = store.resume_holds_for_attention("att_1", outcome="answered")
    assert [h["id"] for h in done] == [hold["id"]]
    assert store.get(hold["id"])["status"] == "resumed"
    assert store.resume_holds_for_attention("att_1", outcome="dismissed") == []


def test_create_hold_with_question_makes_attention(client, holds_env):
    created = client.post(
        _holds_url(holds_env["routine_id"]),
        json={"reason": "which branch?", "question": "Ship branch A or B?"},
    )
    assert created.status_code == 200, created.text
    body = created.json()
    assert body["hold"]["status"] == "held"
    assert body["attention"]["question"] == "Ship branch A or B?"
    assert body["hold"]["attention_id"] == body["attention"]["id"]
    listed = client.get(_holds_url(holds_env["routine_id"]))
    assert len(listed.json()["holds"]) == 1


def test_answer_attention_resumes_hold(client, holds_env):
    created = client.post(
        _holds_url(holds_env["routine_id"]),
        json={"reason": "which branch?", "question": "Ship branch A or B?"},
    )
    attention_id = created.json()["attention"]["id"]
    answered = client.post(
        f"/api/v1/bots/attention/{attention_id}/answer",
        json={"answer": "Ship A."},
    )
    assert answered.status_code == 200, answered.text
    listed = client.get(_holds_url(holds_env["routine_id"]), params={"status": "all"})
    assert listed.json()["holds"][0]["status"] == "resumed"


def test_dismiss_attention_resolves_dismissed(client, holds_env):
    created = client.post(
        _holds_url(holds_env["routine_id"]),
        json={"reason": "which branch?", "question": "Ship branch A or B?"},
    )
    attention_id = created.json()["attention"]["id"]
    dismissed = client.post(f"/api/v1/bots/attention/{attention_id}/dismiss")
    assert dismissed.status_code == 200, dismissed.text
    listed = client.get(_holds_url(holds_env["routine_id"]), params={"status": "all"})
    assert listed.json()["holds"][0]["status"] == "dismissed"


def test_resume_route_and_conflicts(client, holds_env):
    created = client.post(
        _holds_url(holds_env["routine_id"]),
        json={"reason": "waiting on a scan"},
    )
    hold_id = created.json()["hold"]["id"]
    resumed = client.post(
        _holds_url(holds_env["routine_id"]) + f"/{hold_id}/resume",
        json={"note": "scan arrived"},
    )
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["hold"]["resolution_note"] == "scan arrived"
    again = client.post(
        _holds_url(holds_env["routine_id"]) + f"/{hold_id}/resume", json={}
    )
    assert again.status_code == 409
    assert client.get(_holds_url("no-such-routine")).status_code == 404
    empty = client.post(_holds_url(holds_env["routine_id"]), json={"reason": "  "})
    assert empty.status_code == 422
    ghost = client.post(
        _holds_url(holds_env["routine_id"]),
        json={"reason": "x", "attention_id": "att_nope"},
    )
    assert ghost.status_code == 404


def test_hold_written_to_routine_history(client, holds_env):
    client.post(
        _holds_url(holds_env["routine_id"]),
        json={"reason": "waiting on a scan"},
    )
    history = client.get(
        f"/api/v1/bots/alpha/routines/{holds_env['routine_id']}/history"
    )
    assert history.status_code == 200, history.text
    records = history.json()["history"]
    assert any(
        r.get("status") == "held" and "hold" in str(r.get("trigger_id"))
        for r in records
    )
