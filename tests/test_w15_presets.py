"""W15 role & skill preset pack tests (offline; temp bot registry)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server import auth as auth_mod
from server import feature_flags
from server import role_skill_presets as presets
from server.goal_run.bot_identity import BotRegistry, BotSpec

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture
def flag_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(presets.FLAG_NAME, raising=False)


@pytest.fixture
def flag_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(presets.FLAG_NAME, "1")


@pytest.fixture
def registry_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "bots.json"
    monkeypatch.setenv("VEYA_BOT_REGISTRY_PATH", str(path))
    reg = BotRegistry(path)
    reg.register(BotSpec(bot_id="mine", owner_id="alice", name="Mine", instructions="Always answer in Chinese."))
    reg.register(BotSpec(bot_id="theirs", owner_id="bob", name="Theirs"))
    return path


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    from server.routes import role_skill_presets as routes

    def require_user(*_a: Any) -> dict[str, Any]:
        return {"user_id": "alice"}

    monkeypatch.setattr(auth_mod, "require_user", require_user)
    app = FastAPI()
    app.include_router(routes.router)
    return TestClient(app)


# ── loading ────────────────────────────────────────────────────────────────


def test_roles_load_with_provenance() -> None:
    roles = presets.load_roles()
    assert 20 <= len(roles) <= 30
    cats = {r.category for r in roles}
    assert {"engineering", "product", "design", "marketing", "operations", "research", "writing"} <= cats
    for role in roles:
        assert role.license == "MIT"
        assert role.source.startswith("https://github.com/msitarzewski/agency-agents/blob/")
        assert "AgentLand Contributors" in role.copyright
        assert role.summary_zh
        assert role.body and role.name


def test_skills_load_with_provenance() -> None:
    skills = presets.load_skills()
    assert len(skills) >= 20
    ids = {s.preset_id for s in skills}
    assert {"test-driven-development", "code-review-and-quality", "planning-and-task-breakdown"} <= ids
    for skill in skills:
        assert skill.license == "MIT"
        assert "Addy Osmani" in skill.copyright
        assert skill.source.startswith("https://github.com/addyosmani/agent-skills/blob/")


def test_unknown_or_bad_ids_rejected() -> None:
    with pytest.raises(presets.PresetError):
        presets.get_role("no-such-role")
    with pytest.raises(presets.PresetError):
        presets.get_skill("../etc/passwd")


# ── flag ───────────────────────────────────────────────────────────────────


def test_flag_registered_default_off(flag_off: None) -> None:
    names = [f.name for f in feature_flags.FLAGS]
    assert presets.FLAG_NAME in names
    spec = next(f for f in feature_flags.FLAGS if f.name == presets.FLAG_NAME)
    assert spec.default is False
    assert presets.flag_enabled() is False


def test_list_routes_report_unavailable_when_off(flag_off: None, client: TestClient) -> None:
    for path in ("/api/v1/presets/roles", "/api/v1/presets/skills"):
        body = client.get(path).json()
        assert body["available"] is False
        assert presets.FLAG_NAME in body["reason"]
        assert body["items"] == []


def test_list_routes_when_on(flag_on: None, client: TestClient) -> None:
    roles = client.get("/api/v1/presets/roles").json()
    assert roles["available"] is True and len(roles["items"]) >= 20
    assert {"id", "name", "summary_zh", "license", "source"} <= set(roles["items"][0])
    skills = client.get("/api/v1/presets/skills").json()
    assert skills["available"] is True and len(skills["items"]) >= 20


# ── apply ──────────────────────────────────────────────────────────────────


def test_apply_refused_when_flag_off(flag_off: None, registry_path: Path, client: TestClient) -> None:
    resp = client.post("/api/v1/bots/mine/presets/apply", json={"role_id": "sre"})
    assert resp.status_code == 409
    assert BotRegistry(registry_path).get("mine").instructions == "Always answer in Chinese."


def test_apply_non_owner_is_404(flag_on: None, registry_path: Path, client: TestClient) -> None:
    for bot in ("theirs", "ghost"):
        resp = client.post(f"/api/v1/bots/{bot}/presets/apply", json={"role_id": "sre"})
        assert resp.status_code == 404
    assert BotRegistry(registry_path).get("theirs").instructions == ""


def test_apply_writes_role_and_skills(flag_on: None, registry_path: Path, client: TestClient) -> None:
    resp = client.post(
        "/api/v1/bots/mine/presets/apply",
        json={"role_id": "backend-architect", "skill_ids": ["test-driven-development"]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["role"] == "backend-architect"
    assert body["skills_added"] == ["preset:test-driven-development"]
    spec = BotRegistry(registry_path).get("mine")
    assert spec.instructions.startswith(presets.ROLE_BLOCK_START)
    assert presets.ROLE_BLOCK_END in spec.instructions
    assert "Backend Architect" in spec.instructions
    assert "third-party template" in spec.instructions  # untrusted wrapper
    assert spec.instructions.endswith("Always answer in Chinese.")  # user text kept
    assert len(spec.instructions) <= presets.INSTRUCTIONS_MAX
    assert spec.available_skill_ids == ["preset:test-driven-development"]

    # Re-apply another role: block replaced, not duplicated; skills idempotent.
    resp = client.post(
        "/api/v1/bots/mine/presets/apply",
        json={"role_id": "sre", "skill_ids": ["test-driven-development"]},
    )
    assert resp.status_code == 200
    spec = BotRegistry(registry_path).get("mine")
    assert spec.instructions.count(presets.ROLE_BLOCK_START) == 1
    assert "Backend Architect" not in spec.instructions
    assert spec.available_skill_ids == ["preset:test-driven-development"]
    assert resp.json()["skills_added"] == []


def test_apply_unknown_role_is_422(flag_on: None, registry_path: Path, client: TestClient) -> None:
    resp = client.post("/api/v1/bots/mine/presets/apply", json={"role_id": "nope"})
    assert resp.status_code == 422
    resp = client.post("/api/v1/bots/mine/presets/apply", json={})
    assert resp.status_code == 422


def test_role_block_fits_existing_patch_limit() -> None:
    spec = BotSpec(bot_id="x", owner_id="a", instructions="y" * 3000)
    new, summary = presets.apply_to_spec(spec, role_id="product-manager")
    assert len(new.instructions) <= presets.INSTRUCTIONS_MAX
    assert summary["role_truncated"] is True
    full = BotSpec(bot_id="x", owner_id="a", instructions="y" * 3990)
    with pytest.raises(presets.PresetError):
        presets.apply_to_spec(full, role_id="product-manager")


# ── attribution / no executable content ─────────────────────────────────────


def test_third_party_notice_present() -> None:
    text = (REPO / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")
    assert "msitarzewski/agency-agents" in text
    assert "addyosmani/agent-skills" in text
    assert "Copyright (c) 2025 AgentLand Contributors" in text
    assert "Copyright (c) 2025 Addy Osmani" in text


def test_preset_pack_contains_no_scripts() -> None:
    root = REPO / "config" / "presets"
    files = [p for p in root.rglob("*") if p.is_file()]
    assert files
    for path in files:
        assert path.suffix == ".md", path
        assert not os.access(path, os.X_OK), path


def test_apply_never_executes_anything(monkeypatch: pytest.MonkeyPatch) -> None:
    import subprocess

    def boom(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("preset code must not spawn processes")

    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setattr(subprocess, "Popen", boom)
    monkeypatch.setattr(os, "system", boom)
    spec = BotSpec(bot_id="x", owner_id="a")
    new, _ = presets.apply_to_spec(spec, role_id="sre", skill_ids=["idea-refine"])
    assert new.available_skill_ids == ["preset:idea-refine"]
