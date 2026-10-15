"""Tests for TDX Feature 2: Agent Publish Permissions (VEYA_TDX_AGENT_PUBLISH_PERMS)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from server.app import app
from server.routes.tdx import check_publish_permission


client = TestClient(app)


def test_publish_perms_flag_off(monkeypatch):
    monkeypatch.delenv("VEYA_TDX_AGENT_PUBLISH_PERMS", raising=False)
    monkeypatch.setenv("VEYA_OWNER_USER_ID", "owner-123")
    monkeypatch.setattr("server.auth.require_user", lambda: {"user_id": "owner-123", "is_owner": True})
    resp = client.get("/api/v1/tdx/publish-perms")
    assert resp.status_code == 404


def test_publish_perms_flow(monkeypatch):
    monkeypatch.setenv("VEYA_TDX_AGENT_PUBLISH_PERMS", "true")
    monkeypatch.setenv("VEYA_OWNER_USER_ID", "owner-123")
    monkeypatch.setattr("server.auth.require_user", lambda: {"user_id": "owner-123", "is_owner": True})

    # Get perms
    resp = client.get("/api/v1/tdx/publish-perms", headers={"Authorization": "Bearer owner"})
    assert resp.status_code == 200
    assert "permissions" in resp.json()

    # Update perms for agent-x
    resp_put = client.put("/api/v1/tdx/publish-perms/agent-x", json={"push": True, "merge": False, "tag": False, "deploy": False}, headers={"Authorization": "Bearer owner"})
    assert resp_put.status_code == 200
    assert resp_put.json()["permissions"]["push"] is True

    # Check enforcement: push allowed, merge denied
    check_publish_permission("agent-x", "push")
    with pytest.raises(PermissionError):
        check_publish_permission("agent-x", "merge")


def test_publish_perms_flag_off_bypasses(monkeypatch):
    monkeypatch.delenv("VEYA_TDX_AGENT_PUBLISH_PERMS", raising=False)
    # When flag off, check_publish_permission should not raise even if disabled
    check_publish_permission("unauthorized-agent", "deploy")
