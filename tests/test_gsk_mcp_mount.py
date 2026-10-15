from fastapi.testclient import TestClient


def test_governed_mcp_router_is_mounted_by_legacy_gateway():
    # Importing this module must bind the legacy transport onto the canonical app.
    from veya.server.app import app

    response = TestClient(app).get("/mcp/health")
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "ok"
