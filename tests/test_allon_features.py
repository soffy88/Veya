# 3O-IO-ALLOW — tests/test_allon_features.py
"""ALLON: wiring tests for the default-OFF switches turned on in this branch.

Every check asserts that, with the flag ON, the feature's owner-checked REST
route / backend behavior is live (not 404 / not no-op). Feature-specific unit
tests already live in their own wave files; this suite is the cross-cutting
"switch ON → surface works" contract for ALLON.

Run: .venv/bin/python -m pytest tests/test_allon_features.py -q
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server import auth as auth_mod
from server import feature_flags


@pytest.fixture
def owner_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A TestClient with a logged-in OWNER user and a temp bot registry."""
    monkeypatch.setenv("VEYA_BOT_REGISTRY_PATH", str(tmp_path / "bot_registry.json"))
    monkeypatch.setenv("VEYA_BOTS_ROOT", str(tmp_path / "bots"))
    monkeypatch.setenv("VEYA_PROJECTS_ROOT", str(tmp_path / "projects"))
    monkeypatch.setenv("VEYA_TAILNET_OWNER_USER_ID", "owner-uid")

    user = {"user_id": "owner-uid", "username": "owner", "is_owner": True}

    def _owner():
        return dict(user)

    # Capture the REAL auth functions BEFORE any patching so dependency_overrides
    # is keyed on the exact objects FastAPI registered in the route Depends().
    real_require_user = auth_mod.require_user
    real_get_current_user = auth_mod.get_current_user

    app = FastAPI()
    from server.routes import bot_team, projects, role_skill_presets, tailnet

    app.include_router(bot_team.router)
    app.include_router(projects.router)
    app.include_router(role_skill_presets.router)
    app.include_router(tailnet.router)
    # FastAPI dependency overrides (keyed on the original registered functions).
    app.dependency_overrides[real_require_user] = _owner
    app.dependency_overrides[real_get_current_user] = _owner
    # Some route helpers (e.g. presets _require_user) call require_user() as a
    # plain function, bypassing the dependency graph — patch the module attr too.
    monkeypatch.setattr(auth_mod, "require_user", _owner)
    monkeypatch.setattr(auth_mod, "get_current_user", _owner)
    return TestClient(app)


# ── W15 role & skill presets ──────────────────────────────────────────


def test_w15_presets_list_and_apply_on(monkeypatch: pytest.MonkeyPatch, owner_client: TestClient):
    monkeypatch.setenv("VEYA_ROLE_SKILL_PRESETS", "1")
    c = owner_client

    roles = c.get("/api/v1/presets/roles")
    assert roles.status_code == 200 and roles.json()["available"] is True
    assert roles.json()["items"], "committed role presets should be listed when on"

    skills = c.get("/api/v1/presets/skills")
    assert skills.status_code == 200 and skills.json()["available"] is True

    # Build a bot owned by the caller, then apply a real role preset.
    created = c.post(
        "/api/v1/bots",
        json={"bot_id": "preset-bot", "name": "preset-bot", "instructions": "base instructions"},
    )
    assert created.status_code == 200
    bot_id = created.json()["bot"]["bot_id"]

    role_id = roles.json()["items"][0]["id"]
    applied = c.post(
        f"/api/v1/bots/{bot_id}/presets/apply",
        json={"role_id": role_id},
    )
    assert applied.status_code == 200
    assert "role-preset" in applied.json()["bot"]["instructions"]


# ── W16 project brain ─────────────────────────────────────────────────


def test_w16_project_create_list_brief_on(
    monkeypatch: pytest.MonkeyPatch, owner_client: TestClient
):
    monkeypatch.setenv("VEYA_PROJECT_BRAIN", "1")
    c = owner_client

    created = c.post("/api/v1/projects", json={"name": "alpha"})
    assert created.status_code == 200
    pid = created.json()["project"]["id"]

    listed = c.get("/api/v1/projects")
    assert listed.status_code == 200
    assert any(p["id"] == pid for p in listed.json()["projects"])

    brief = c.get(f"/api/v1/projects/{pid}/brief")
    assert brief.status_code == 200 and "brief" in brief.json()


# ── W17 SSH hosts (REST surface above W13 remote control) ─────────────


def test_w17_ssh_hosts_probe_on(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("VEYA_REMOTE_CONTROL", "1")
    monkeypatch.setenv("VEYA_SSH_HOSTS", "1")
    monkeypatch.setenv("VEYA_REMOTE_CONTROL_HOSTS_PATH", str(tmp_path / "hosts.json"))
    monkeypatch.setenv("VEYA_REMOTE_CONTROL_POLICY_PATH", str(tmp_path / "policy.json"))
    monkeypatch.setenv("VEYA_SSH_HOSTS_CM_DIR", str(tmp_path / "cm"))

    from server import auth as a2
    from server.remote_control import hosts as hosts_mod
    from server.routes import remote_control as rc_routes

    root = tmp_path / "w"
    root.mkdir()
    hosts_mod.save_host(
        hosts_mod.RemoteHost(
            host_id="lap",
            display_name="L",
            tailnet_ip="100.64.0.7",
            owner_id="u1",
            write_root=str(root),
        )
    )

    app = FastAPI()
    app.include_router(rc_routes.router)
    app.dependency_overrides[a2.require_user] = lambda: {"user_id": "u1"}
    app.dependency_overrides[a2.get_current_user] = lambda: {"user_id": "u1"}
    client = TestClient(app)

    # The SSH section is gated on VEYA_SSH_HOSTS; probe must not 404 on the
    # flag itself (it 200s the surface; identity/runner are faked).
    from server.remote_control import identity as identity_mod
    from server.remote_control import service as service_mod

    monkeypatch.setattr(
        service_mod,
        "check_identity",
        lambda host, action: identity_mod.PeerIdentity(node_name="lap", resolved=True),
    )
    probe = client.get("/api/v1/remote/hosts/lap/probe")
    # Surface is live (200/422 depending on the faked runner); never a flag-off 404.
    assert probe.status_code != 404
    # The hosts list (W13) must still answer when SSH flag is on.
    assert client.get("/api/v1/remote/hosts").status_code == 200


# ── FastClaw: public share fork + toggle + public fetch ───────────────


def test_fastclaw_public_share_fork_toggle_on(
    monkeypatch: pytest.MonkeyPatch, owner_client: TestClient
):
    monkeypatch.setenv("VEYA_BOT_PUBLIC_SHARE", "1")
    c = owner_client

    created = c.post(
        "/api/v1/bots",
        json={"bot_id": "share-bot", "name": "share-bot", "instructions": "x"},
    )
    assert created.status_code == 200
    bot_id = created.json()["bot"]["bot_id"]

    forked = c.post(f"/api/v1/bots/{bot_id}/fork", json={"name": "share-bot-fork"})
    assert forked.status_code == 200
    fork_id = forked.json()["bot"]["bot_id"]

    toggled = c.post(
        f"/api/v1/bots/{fork_id}/public_share",
        json={"enabled": True},
    )
    assert toggled.status_code == 200 and toggled.json()["public_share"] is True

    public = c.get(f"/api/v1/bots/{fork_id}/public")
    assert public.status_code == 200 and public.json()["bot"]["public_share"] is True

    # Fetching a public bot that is NOT shared → 404 (guest isolation intact).
    not_shared = c.get(f"/api/v1/bots/{bot_id}/public")
    assert not_shared.status_code == 404


# ── Backend-only feature flags read as enabled (no UI needed) ─────────


@pytest.mark.parametrize(
    "flag",
    [
        "VEYA_APPROVAL_ASK_ONCE",
        "VEYA_IM_SANDBOX_ENFORCE",
        "VEYA_SKILL_PROGRESSIVE",
        "VEYA_WORKSPACE_URI",
        "VEYA_MEMORY_UI",
        "VEYA_PROJECT_BRAIN",
        "VEYA_ROLE_SKILL_PRESETS",
        "VEYA_SSH_HOSTS",
        "VEYA_BOT_PUBLIC_SHARE",
    ],
)
def test_feature_flags_enabled_when_set(monkeypatch: pytest.MonkeyPatch, flag: str):
    monkeypatch.setenv(flag, "1")
    assert feature_flags.enabled(flag) is True


# VEYA_DEEPEVAL and VEYA_TAILNET_CHANNEL are env-only switches (read via
# os.getenv in their own modules, not registered in feature_flags.FLAGS), so they
# are asserted through their own enabled() predicates instead.
def test_deepeval_and_tailnet_channel_flags_env_only(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("VEYA_DEEPEVAL", "1")
    from server import deepeval_adapter as da

    assert da.enabled() is True

    monkeypatch.setenv("VEYA_TAILNET_CHANNEL", "1")
    from server import tailnet_channel as tc

    assert tc.enabled() is True


# ── W18 DeepEval: flag ON but optional package absent → graceful no-op ──


def test_w18_deepeval_enabled_but_package_absent(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("VEYA_DEEPEVAL", "1")
    from server import deepeval_adapter as da

    assert da.enabled() is True
    # `deepeval` is not installed in the shared .venv → available() is False,
    # so the supervisor path no-ops. No install, no cost.
    assert da.available() is False
    # score_agent_result on an existing result returns the graceful unknown shape.
    result = da.score_agent_result(SimpleNamespace(input="q"), {"reply": "a", "tool_calls": []})
    assert result["enabled"] is False


# ── W20 Tailnet: owner-only status surface live when channel ON ───────


def test_w20_tailnet_status_owner_on(monkeypatch: pytest.MonkeyPatch, owner_client: TestClient):
    monkeypatch.setenv("VEYA_TAILNET_CHANNEL", "1")
    monkeypatch.setenv("VEYA_TAILNET_OWNER_USER_ID", "owner-uid")
    c = owner_client

    status = c.get("/api/v1/tailnet/status")
    assert status.status_code == 200
    body = status.json()
    assert body["enabled"] is True
    assert body["default_port"] == 7767
    # Default policy is deny-all (allowlist empty) — fail-closed.
    assert body["allow_count"] == 0


def test_w20_tailnet_status_forbidden_for_non_owner(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    monkeypatch.setenv("VEYA_TAILNET_CHANNEL", "1")
    # No owner id configured and the caller is not an owner/admin → 403.
    monkeypatch.delenv("VEYA_TAILNET_OWNER_USER_ID", raising=False)
    monkeypatch.delenv("VEYA_OWNER_USER_ID", raising=False)

    from server import auth as a3
    from server.routes import tailnet as t_routes

    app = FastAPI()
    app.include_router(t_routes.router)
    app.dependency_overrides[a3.require_user] = lambda: {"user_id": "someone", "username": "x"}
    app.dependency_overrides[a3.get_current_user] = lambda: {"user_id": "someone", "username": "x"}
    client = TestClient(app)
    assert client.get("/api/v1/tailnet/status").status_code == 403


# ── IM sandbox enforcement: non-owner IM turn denied when ON ──────────


def test_im_sandbox_enforce_denies_unsandboxed_exec(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("VEYA_IM_SANDBOX_ENFORCE", "1")
    from server.channels import sandbox_enforce as se

    se.bind_im_turn(is_im=True, is_owner=False, platform="telegram", bot_id="b1")
    try:
        assert se.is_im_non_owner_turn() is True
        reason = se.check_im_sandbox_tool("system_shell_exec", {})
        assert reason is not None and "denied" in reason
        # Owner turn is unaffected.
    finally:
        se.reset_im_turn(se._im_turn_ctx.set({}))
