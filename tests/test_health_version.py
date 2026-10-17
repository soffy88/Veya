"""Regression tests for the deploy identity exposed by ``GET /health``."""

from __future__ import annotations

import importlib

from fastapi.testclient import TestClient


def test_health_reports_build_sha(monkeypatch) -> None:
    monkeypatch.setenv("VEYA_BUILD_SHA", "deadbeef")
    import server.app as app_module

    app_module = importlib.reload(app_module)
    client = TestClient(app_module.app, base_url="http://localhost")
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["version"] == "0.6.0-deadbeef"


def test_health_without_build_sha_stays_release_version(monkeypatch) -> None:
    monkeypatch.delenv("VEYA_BUILD_SHA", raising=False)
    import server.app as app_module

    app_module = importlib.reload(app_module)
    client = TestClient(app_module.app, base_url="http://localhost")
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["version"] == "0.6.0"
