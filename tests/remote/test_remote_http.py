"""HTTP transport test for the remote MCP route (POST /mcp, GET /mcp/health)."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

import server.routes.remote_mcp as route
from tests.remote.test_remote_gateway import FakeExecutor, make_gateway


def _client(tmp_path: Path, monkeypatch) -> tuple[TestClient, str]:
    gateway, secret, _audit, _adapter = make_gateway(tmp_path, FakeExecutor(tmp_path / "wt"))
    monkeypatch.setattr(route, "_gateway", gateway)
    app = FastAPI()
    app.include_router(route.router)
    return TestClient(app), secret


def test_health_endpoint(tmp_path: Path, monkeypatch) -> None:
    client, secret = _client(tmp_path, monkeypatch)
    response = client.get("/mcp/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert secret not in json.dumps(body)


def test_jsonrpc_over_http(tmp_path: Path, monkeypatch) -> None:
    client, secret = _client(tmp_path, monkeypatch)
    headers = {"Authorization": f"Bearer {secret}"}

    init = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        headers=headers,
    ).json()
    session_id = init["result"]["sessionId"]

    listing = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/list",
            "params": {"session_id": session_id},
        },
        headers=headers,
    ).json()
    assert any(tool["name"] == "file.read" for tool in listing["result"]["tools"])

    call = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "workspace.info",
                "arguments": {},
                "session_id": session_id,
            },
        },
        headers=headers,
    ).json()
    assert call["result"]["isError"] is False


def test_http_auth_fail_closed(tmp_path: Path, monkeypatch) -> None:
    client, _secret = _client(tmp_path, monkeypatch)
    response = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
    ).json()
    assert response["error"]["data"]["error_code"] == "AUTH_DENIED"


def test_http_session_terminate(tmp_path: Path, monkeypatch) -> None:
    client, secret = _client(tmp_path, monkeypatch)
    headers = {"Authorization": f"Bearer {secret}"}
    init = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        headers=headers,
    ).json()
    session_id = init["result"]["sessionId"]

    terminated = client.delete("/mcp", headers={**headers, "Mcp-Session-Id": session_id})
    assert terminated.status_code == 204

    # the terminated session can no longer be used
    listing = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        headers={**headers, "Mcp-Session-Id": session_id},
    ).json()
    assert listing["error"]["data"]["error_code"] == "NOT_FOUND"


def test_http_notification_accepted(tmp_path: Path, monkeypatch) -> None:
    client, secret = _client(tmp_path, monkeypatch)
    response = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
        headers={"Authorization": f"Bearer {secret}"},
    )
    assert response.status_code == 202
