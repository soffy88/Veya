"""W11 group B: the read-only endpoints the bot landing web UI depends on.

Group B owns the web surface. It needed exactly one thing the server did not
have: a list of models the create-bot picker can show. ``/api/v1/providers``
projects the same data but is flag-gated off by default, so on a default
deployment the picker would be empty — this covers the minimal catalog route
added for it, including the gate that matters (a client that creates bots must
be logged in).

No bot turn / verification / routine code is exercised here; those belong to
group A1/A2.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def auth_headers():
    from server import auth as auth_mod

    username = f"w11b_{uuid.uuid4().hex[:8]}"
    user = auth_mod.create_user(username, "password_123456")
    token = auth_mod.issue_token(user["user_id"])
    return {"Authorization": f"Bearer {token}", "user_id": user["user_id"]}


def _models(client: TestClient, headers: dict[str, str]) -> list[dict]:
    res = client.get("/api/v1/models", headers=headers)
    assert res.status_code == 200, res.text
    payload = res.json()
    return list(payload.get("models") or [])


def test_models_catalog_requires_login() -> None:
    """Creating a bot is a logged-in action (W11 D2), so its picker is too."""

    from server.app import app

    client = TestClient(app)
    assert client.get("/api/v1/models").status_code == 401


def test_models_catalog_offers_the_server_default(auth_headers) -> None:
    """The picker must be able to preselect the model a default bot actually uses."""

    from server.app import app

    client = TestClient(app)
    res = client.get("/api/v1/models", headers=auth_headers)
    assert res.status_code == 200
    payload = res.json()
    models = _models(client, auth_headers)

    assert payload["default_model"], payload
    assert payload["default_provider"] == "veya1.2"
    assert models, "an empty catalog would leave the picker with nothing to show"
    # The server default is selectable, not only mentioned as the default.
    assert any(row["id"] == payload["default_model"] and row["default"] for row in models), models


def test_models_catalog_lists_provider_models(auth_headers) -> None:
    """Builtin providers contribute their own models, each naming its provider."""

    from server.app import app

    client = TestClient(app)
    models = _models(client, auth_headers)
    by_provider: dict[str, set[str]] = {}
    for row in models:
        by_provider.setdefault(row["provider"], set()).add(row["id"])
        assert row["provider_label"], row

    assert "openai" in by_provider
    assert "gpt-4o-mini" in by_provider["openai"]
    # opencode-go is in the LLM table but not the provider registry: dropping it
    # would hide the main model the always-on set runs on.
    assert "deepseek-v4.1-flash" in by_provider.get("opencode-go", set())


def test_models_catalog_has_no_duplicates(auth_headers) -> None:
    """One (provider, model) pair is one row — a picker must not repeat a choice."""

    from server.app import app

    client = TestClient(app)
    models = _models(client, auth_headers)
    pairs = [(row["provider"], row["id"]) for row in models]
    assert len(pairs) == len(set(pairs))


def test_models_catalog_is_read_only(auth_headers) -> None:
    """A catalog writes nothing: every mutating verb is refused by the router."""

    from server.app import app

    client = TestClient(app)
    for method in ("POST", "PATCH", "PUT", "DELETE"):
        res = client.request(method, "/api/v1/models", headers=auth_headers, json={})
        assert res.status_code == 405, (method, res.status_code)
