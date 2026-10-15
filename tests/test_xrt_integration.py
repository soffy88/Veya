"""XRT profile registration and REST authentication regression tests."""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from runtime.coding.cli_agents import XRT_RUNTIME_PROFILES, safe_runtime_env, xrt_feature_enabled
from server import auth as auth_mod
from server.routes import cli_agents as cli_agents_routes
from server.routes import xrt as xrt_routes
from veya.remote.executor_adapter import list_executor_adapters
from veya.remote.executor_adapters import register_default_adapters


def make_client() -> tuple[TestClient, FastAPI]:
    app = FastAPI()
    app.include_router(xrt_routes.router)
    app.include_router(cli_agents_routes.router)
    return TestClient(app), app


def test_xrt_profiles_cover_all_twelve_cli_runtimes() -> None:
    assert set(XRT_RUNTIME_PROFILES) == {
        "devin", "claude_code", "codex", "cursor_agent", "gemini", "copilot",
        "qwen", "droid", "cline", "opencode", "grok", "hermes",
    }


def test_xrt_flag_defaults_off(monkeypatch) -> None:
    monkeypatch.delenv("VEYA_XRT_RUNTIMES", raising=False)
    assert xrt_feature_enabled() is False
    monkeypatch.setenv("VEYA_XRT_RUNTIMES", "0")
    assert xrt_feature_enabled() is False
    monkeypatch.setenv("VEYA_XRT_RUNTIMES", "devin,copilot")
    assert xrt_feature_enabled() is True


def test_xrt_disabled_returns_404_before_auth(monkeypatch) -> None:
    monkeypatch.delenv("VEYA_XRT_RUNTIMES", raising=False)
    client, _app = make_client()
    response = client.get("/api/v1/xrt/runtimes")
    assert response.status_code == 404


def test_xrt_no_token_returns_401_when_enabled(monkeypatch) -> None:
    monkeypatch.setenv("VEYA_XRT_RUNTIMES", "devin")
    client, _app = make_client()
    response = client.get("/api/v1/xrt/runtimes")
    assert response.status_code == 401


def test_xrt_non_owner_returns_403(monkeypatch) -> None:
    monkeypatch.setenv("VEYA_XRT_RUNTIMES", "devin")
    client, app = make_client()
    app.dependency_overrides[auth_mod.require_user] = lambda: {"user_id": "test-user"}
    try:
        response = client.get("/api/v1/xrt/runtimes")
        assert response.status_code == 403
    finally:
        app.dependency_overrides.clear()


def test_cli_agent_routes_are_owner_only(monkeypatch) -> None:
    client, app = make_client()
    app.dependency_overrides[auth_mod.require_user] = lambda: {"user_id": "non-owner"}
    try:
        listed = client.get("/api/v1/agents/cli")
        registered = client.post(
            "/api/v1/agents/cli/custom",
            json={"name": "test-profile", "command_template": "fake-cli {prompt}"},
        )
        assert listed.status_code == 403
        assert registered.status_code == 403
    finally:
        app.dependency_overrides.clear()


def test_safe_runtime_env_excludes_backend_secrets(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "not-a-real-key")
    monkeypatch.setenv("VENDOR_ACCESS_TOKEN", "not-a-real-token")
    monkeypatch.setenv("INTERNAL_SERVICE_SECRET", "not-a-real-secret")
    child_env = safe_runtime_env("devin")
    forbidden = {
        name for name in child_env
        if name.endswith(("_KEY", "_TOKEN", "_SECRET"))
    }
    assert forbidden == set()
    assert {"PATH", "HOME"}.issubset(child_env)


def test_xrt_extra_adapters_are_opt_in_and_per_runtime(monkeypatch) -> None:
    extra = set(XRT_RUNTIME_PROFILES) - {"claude_code", "codex", "opencode", "grok"}
    monkeypatch.setenv("VEYA_XRT_RUNTIMES", "0")
    register_default_adapters()
    assert not extra.intersection(list_executor_adapters())
    monkeypatch.setenv("VEYA_XRT_RUNTIMES", "devin")
    register_default_adapters()
    assert "devin" in list_executor_adapters()
    assert not (extra - {"devin"}).intersection(list_executor_adapters())
    monkeypatch.setenv("VEYA_XRT_RUNTIME_DEVIN", "0")
    register_default_adapters()
    assert "devin" not in list_executor_adapters()
    monkeypatch.setenv("VEYA_XRT_RUNTIMES", "0")
    register_default_adapters()
