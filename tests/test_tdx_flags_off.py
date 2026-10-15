"""Tests ensuring TDX flags OFF maintain exact parity with main (all return 404/disabled)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from server.app import app


client = TestClient(app)


def test_all_tdx_flags_off(monkeypatch):
    monkeypatch.delenv("VEYA_TDX_NEEDS_YOU", raising=False)
    monkeypatch.delenv("VEYA_TDX_AGENT_PUBLISH_PERMS", raising=False)
    monkeypatch.delenv("VEYA_TDX_RUN_HISTORY", raising=False)
    monkeypatch.setenv("VEYA_OWNER_USER_ID", "owner-123")
    monkeypatch.setattr("server.auth.require_user", lambda: {"user_id": "owner-123", "is_owner": True})

    assert client.get("/api/v1/tdx/needs-you").status_code == 404
    assert client.get("/api/v1/tdx/publish-perms").status_code == 404
    assert client.put("/api/v1/tdx/publish-perms/agent-1", json={"push": True}).status_code == 404
    assert client.get("/api/v1/tdx/runs").status_code == 404
    assert client.get("/api/v1/tdx/runs/t1/rounds").status_code == 404
    assert client.get("/api/v1/tdx/runs/t1/compare").status_code == 404
