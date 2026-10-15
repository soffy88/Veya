from fastapi import FastAPI
from fastapi.testclient import TestClient

from server import auth as auth_mod
from server.routes.settings import router


def _app():
    app = FastAPI()
    app.include_router(router)
    return app


def test_artifact_settings_reject_unauthenticated_write(tmp_path, monkeypatch):
    monkeypatch.setenv("VEYA_SETTINGS_PATH", str(tmp_path / "settings.json"))
    with TestClient(_app()) as client:
        response = client.put("/api/v1/settings/artifacts", json={"enabled": True})
    assert response.status_code == 401


def test_artifact_settings_are_owner_dependency_gated(tmp_path, monkeypatch):
    monkeypatch.setenv("VEYA_SETTINGS_PATH", str(tmp_path / "settings.json"))
    app = _app()
    app.dependency_overrides[auth_mod.require_owner] = lambda: {
        "user_id": "security-owner",
        "username": "security-owner",
        "is_owner": True,
    }
    with TestClient(app) as client:
        response = client.put("/api/v1/settings/artifacts", json={"enabled": True})
        assert response.status_code == 200
        assert response.json() == {"enabled": True}
