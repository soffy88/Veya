"""Product Shell contract: default Bot identity, onboarding, and secret isolation."""

from __future__ import annotations

import json

from fastapi import FastAPI
from fastapi.testclient import TestClient

import server.product_shell as product_shell
from server.routes.product import router


def test_default_bot_is_uninitialized_without_product_config(monkeypatch):
    monkeypatch.setattr(product_shell, "_load_config", lambda: {})

    state = product_shell.read_bot_state()

    assert state["bot"] == {
        "id": "veya-default",
        "name": "Veya Bot",
        "lifecycle": "uninitialized",
    }
    assert state["onboarding"]["required"] is True
    assert state["provider"]["credential"]["ref"] is None


def test_configured_state_never_returns_raw_credential(monkeypatch, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    fixture_marker = "fixture-marker"
    config = {
        "bot": {"onboarding_completed": True},
        "llm": {"provider": "openai", "model": "gpt-test"},
        "providers": {"openai": {"api_key": fixture_marker}},
        "workspace": str(workspace),
    }
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    state = product_shell.read_bot_state(config)

    assert state["bot"]["lifecycle"] == "ready"
    assert state["provider"]["configured"] is True
    assert fixture_marker not in json.dumps(state)


def test_existing_veya_init_config_is_product_ready(monkeypatch, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    state = product_shell.read_bot_state(
        {
            "llm": {"provider": "ollama", "model": "local-test"},
            "workspace": str(workspace),
        }
    )

    assert state["onboarding"]["completed"] is True
    assert state["bot"]["lifecycle"] == "ready"


def test_onboarding_persists_reference_only(monkeypatch, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    saved: dict = {}
    monkeypatch.setattr(product_shell, "_load_config", lambda: {})
    monkeypatch.setattr(product_shell, "_save_config", lambda config: saved.update(config))

    state = product_shell.configure_bot(
        provider="openai",
        model="gpt-test",
        workspace=str(workspace),
        credential_ref="local:web:openai",
    )

    assert state["bot"]["id"] == "veya-default"
    assert saved["providers"]["openai"] == {"credential_ref": "local:web:openai"}
    assert "api_key" not in json.dumps(saved)


def test_product_routes_expose_only_secret_free_state(monkeypatch):
    app = FastAPI()
    app.include_router(router)
    monkeypatch.setattr(product_shell, "_load_config", lambda: {})
    client = TestClient(app)

    response = client.get("/api/v1/bot")

    assert response.status_code == 200
    assert response.json()["bot"]["id"] == "veya-default"
    assert "api_key" not in response.text


def test_product_api_does_not_persist_or_echo_raw_credential(monkeypatch):
    app = FastAPI()
    app.include_router(router)
    persisted: dict = {}
    monkeypatch.setattr(product_shell, "_load_config", lambda: {})
    monkeypatch.setattr(product_shell, "_save_config", lambda config: persisted.update(config))
    client = TestClient(app)

    response = client.post(
        "/api/v1/bot/onboarding",
        json={"provider": "ollama", "model": "fixture-model", "api_key": "fixture-marker"},
    )

    assert response.status_code == 200
    assert "fixture-marker" not in response.text
    assert "api_key" not in json.dumps(persisted)


def test_product_shell_fixture_dogfood(monkeypatch, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    persisted: dict = {}
    monkeypatch.setattr(product_shell, "_load_config", lambda: dict(persisted))
    monkeypatch.setattr(product_shell, "_save_config", lambda config: persisted.update(config))

    assert product_shell.read_bot_state()["bot"]["lifecycle"] == "uninitialized"
    configured = product_shell.configure_bot(
        provider="ollama",
        model="fixture-model",
        workspace=str(workspace),
    )
    restored = product_shell.read_bot_state()

    assert configured["bot"]["id"] == "veya-default"
    assert configured["bot"]["lifecycle"] == "ready"
    assert restored["bot"] == configured["bot"]
    assert restored["recovery"]["tasks"] == "/api/v1/tasks"
