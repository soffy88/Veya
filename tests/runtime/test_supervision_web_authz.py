"""P8A adversarial gate: Web workspace authorization fails closed."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server import auth as auth_mod
from server.routes.supervision import router

CURRENT: dict[str, str] = {"user_id": "userA", "username": "a"}


def _fake_user(authorization: str | None = None) -> dict[str, str]:
    return dict(CURRENT)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    monkeypatch.setenv("VEYA_WEB_WORKSPACE_ROOTS", str(allowed))
    CURRENT.update(user_id="userA", username="a")
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[auth_mod.get_current_user] = _fake_user
    app.dependency_overrides[auth_mod.require_user] = _fake_user
    return TestClient(app), allowed, tmp_path


def _create(client: TestClient, workspace: str, goal: str = "g") -> dict:
    return client.post(
        "/api/v1/supervision/missions",
        json={"goal": goal, "supervision_mode": "auto", "workspace": workspace},
    )


def test_authorized_workspace_create(client):
    c, allowed, _ = client
    resp = _create(c, str(allowed))
    assert resp.status_code == 200, resp.text
    assert resp.json()["mission"]["mission_id"]


def test_unauthorized_workspace_create_is_403_without_side_effects(client):
    c, _allowed, tmp = client
    outside = tmp / "outside"
    outside.mkdir()
    resp = _create(c, str(outside))
    assert resp.status_code == 403
    # no store, no mission, no executor
    assert not (outside / ".veya-project").exists()


def test_missing_workspace_is_denied(client):
    c, _allowed, tmp = client
    resp = _create(c, str(tmp / "does-not-exist"))
    assert resp.status_code == 403


def test_path_traversal_blocked(client):
    c, allowed, tmp = client
    outside = tmp / "outside"
    outside.mkdir()
    resp = _create(c, f"{allowed}/../outside")
    assert resp.status_code == 403
    assert not (outside / ".veya-project").exists()


def test_symlink_escape_blocked(client):
    c, allowed, tmp = client
    outside = tmp / "outside"
    outside.mkdir()
    link = allowed / "escape"
    link.symlink_to(outside, target_is_directory=True)
    resp = _create(c, str(link))
    assert resp.status_code == 403
    assert not (outside / ".veya-project").exists()


def test_system_root_refused(client):
    c, _allowed, _tmp = client
    assert _create(c, "/").status_code == 403


def test_cross_user_read_and_mutation_are_403(client):
    c, allowed, _tmp = client
    mission_id = _create(c, str(allowed)).json()["mission"]["mission_id"]

    CURRENT.update(user_id="userB", username="b")  # attacker knows the mission_id
    for path in (
        f"/api/v1/supervision/missions/{mission_id}",
        f"/api/v1/supervision/missions/{mission_id}/reports/latest",
        f"/api/v1/supervision/missions/{mission_id}/reports/0",
        f"/api/v1/supervision/missions/{mission_id}/reviews",
        f"/api/v1/supervision/missions/{mission_id}/escalations",
        f"/api/v1/supervision/missions/{mission_id}/events?format=json",
    ):
        assert c.get(path).status_code == 403, path
    for path in (
        f"/api/v1/supervision/missions/{mission_id}/run",
        f"/api/v1/supervision/missions/{mission_id}/cancel",
        f"/api/v1/supervision/missions/{mission_id}/mode",
    ):
        assert (
            c.post(path, json={"mode": "auto"} if path.endswith("mode") else {}).status_code == 403
        ), path
    assert (
        c.post(
            f"/api/v1/supervision/missions/{mission_id}/continue",
            json={"review": {"decision": "ACCEPT"}},
        ).status_code
        == 403
    )

    # owner still has access; attacker produced no execution side effect
    executions = allowed / ".veya-project" / "missions" / mission_id / "executions.jsonl"
    assert not executions.exists() or not executions.read_text().strip()
    CURRENT.update(user_id="userA", username="a")
    assert c.get(f"/api/v1/supervision/missions/{mission_id}").status_code == 200


def test_list_only_returns_own_missions(client):
    c, allowed, _tmp = client
    mine = _create(c, str(allowed)).json()["mission"]["mission_id"]
    CURRENT.update(user_id="userB", username="b")
    assert c.get("/api/v1/supervision/missions").json()["missions"] == []
    CURRENT.update(user_id="userA", username="a")
    ids = [m["mission_id"] for m in c.get("/api/v1/supervision/missions").json()["missions"]]
    assert mine in ids


def test_unknown_mission_is_404(client):
    c, _allowed, _tmp = client
    assert c.get("/api/v1/supervision/missions/mission-nope").status_code == 404


def test_owner_marker_is_mission_authority_only(client):
    """Ownership lives on the Mission, not in any Remote MCP token structure."""
    c, allowed, _tmp = client
    mission_id = _create(c, str(allowed)).json()["mission"]["mission_id"]
    payload = json.loads(
        (allowed / ".veya-project" / "missions" / mission_id / "mission.json").read_text()
    )
    assert payload["authority"]["owner_user_id"] == "userA"
    assert "token" not in json.dumps(payload["authority"]).lower()


def test_authorized_absolute_path_inside_root_is_allowed(client):
    c, allowed, _tmp = client
    nested = allowed / "nested"
    nested.mkdir()
    assert _create(c, str(nested)).status_code == 200


def test_no_remote_mcp_token_model_reused():
    """Guard: the Web path must not import the Remote MCP token/session layer."""
    source = Path("server/web_workspace_auth.py").read_text(encoding="utf-8")
    assert "RemoteAuth" not in source and "remote_tokens" not in source
    assert "from server import auth" not in source  # identity comes in as a param
