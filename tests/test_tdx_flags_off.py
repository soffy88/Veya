"""Tests ensuring TDX flags OFF maintain exact parity with main (all return 404/disabled)."""

from __future__ import annotations

from fastapi.testclient import TestClient

from server.app import app


client = TestClient(app)


def test_all_tdx_flags_off(monkeypatch, owner_headers):
    monkeypatch.delenv("VEYA_TDX_NEEDS_YOU", raising=False)
    monkeypatch.delenv("VEYA_TDX_AGENT_PUBLISH_PERMS", raising=False)
    monkeypatch.delenv("VEYA_TDX_RUN_HISTORY", raising=False)
    monkeypatch.setenv("VEYA_OWNER_USER_ID", "owner-123")

    assert client.get("/api/v1/tdx/needs-you", headers=owner_headers).status_code == 404
    assert client.get("/api/v1/tdx/publish-perms", headers=owner_headers).status_code == 404
    assert client.put("/api/v1/tdx/publish-perms/agent-1", json={"push": True}, headers=owner_headers).status_code == 404
    assert client.get("/api/v1/tdx/runs", headers=owner_headers).status_code == 404
    assert client.get("/api/v1/tdx/runs/t1/rounds", headers=owner_headers).status_code == 404
    assert client.get("/api/v1/tdx/runs/t1/compare", headers=owner_headers).status_code == 404
