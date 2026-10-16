from fastapi.testclient import TestClient
from server import auth as auth_mod
from server.app import app


def test_governed_mcp_router_is_mounted_by_legacy_gateway(tmp_path, monkeypatch):
    monkeypatch.setattr(auth_mod, "_DB_PATH", tmp_path / "auth.db")
    auth_mod._init_db()
    client = TestClient(app)
    # SEC hardening: legacy gateway /mcp/health now requires authentication (401) or indicates mount via 401/403
    response = client.get("/mcp/health")
    assert response.status_code in (200, 401, 403), response.text
