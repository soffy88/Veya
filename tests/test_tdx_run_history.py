"""Tests for TDX Feature 3: Run History & Rounds Comparison (VEYA_TDX_RUN_HISTORY)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from server.app import app


client = TestClient(app)


def test_run_history_flag_off(monkeypatch):
    monkeypatch.delenv("VEYA_TDX_RUN_HISTORY", raising=False)
    monkeypatch.setenv("VEYA_OWNER_USER_ID", "owner-123")
    monkeypatch.setattr("server.auth.require_user", lambda: {"user_id": "owner-123", "is_owner": True})
    resp = client.get("/api/v1/tdx/runs")
    assert resp.status_code == 404


def test_run_history_endpoints(monkeypatch):
    monkeypatch.setenv("VEYA_TDX_RUN_HISTORY", "true")
    monkeypatch.setenv("VEYA_OWNER_USER_ID", "owner-123")
    monkeypatch.setattr("server.auth.require_user", lambda: {"user_id": "owner-123", "is_owner": True})

    # GET runs
    resp_runs = client.get("/api/v1/tdx/runs", headers={"Authorization": "Bearer owner"})
    assert resp_runs.status_code == 200
    data = resp_runs.json()
    assert data["enabled"] is True
    assert len(data["runs"]) > 0

    task_id = data["runs"][0]["task_id"]

    # GET rounds
    resp_rounds = client.get(f"/api/v1/tdx/runs/{task_id}/rounds", headers={"Authorization": "Bearer owner"})
    assert resp_rounds.status_code == 200
    assert len(resp_rounds.json()["rounds"]) >= 1

    # GET compare
    resp_compare = client.get(f"/api/v1/tdx/runs/{task_id}/compare?a=1&b=2", headers={"Authorization": "Bearer owner"})
    assert resp_compare.status_code == 200
    assert "diff_comparison" in resp_compare.json()
