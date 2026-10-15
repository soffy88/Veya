"""Tests for TDX Feature 1: Needs You (VEYA_TDX_NEEDS_YOU)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from server.app import app


client = TestClient(app)


def test_needs_you_flag_off(monkeypatch):
    monkeypatch.delenv("VEYA_TDX_NEEDS_YOU", raising=False)
    monkeypatch.setenv("VEYA_OWNER_USER_ID", "owner-123")
    monkeypatch.setattr("server.auth.require_user", lambda: {"user_id": "owner-123", "is_owner": True})
    resp = client.get("/api/v1/tdx/needs-you")
    assert resp.status_code == 404


def test_needs_you_owner_only(monkeypatch):
    monkeypatch.setenv("VEYA_TDX_NEEDS_YOU", "true")
    monkeypatch.setenv("VEYA_OWNER_USER_ID", "owner-123")

    # Anonymous -> 401 via real require_user with no auth header
    resp_anon = client.get("/api/v1/tdx/needs-you")
    assert resp_anon.status_code == 401

    # As non-owner user -> 403
    monkeypatch.setattr("server.auth.require_user", lambda: {"user_id": "other-user", "is_owner": False})
    resp_other = client.get("/api/v1/tdx/needs-you", headers={"Authorization": "Bearer other"})
    assert resp_other.status_code == 403


def test_needs_you_owner_success(monkeypatch):
    monkeypatch.setenv("VEYA_TDX_NEEDS_YOU", "true")
    monkeypatch.setenv("VEYA_OWNER_USER_ID", "owner-123")
    monkeypatch.setattr("server.auth.require_user", lambda: {"user_id": "owner-123", "is_owner": True})

    resp = client.get("/api/v1/tdx/needs-you", headers={"Authorization": "Bearer owner"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["enabled"] is True
    assert "items" in data
