"""Tests for TDX Needs You real approvals and blocked-event projections."""

from __future__ import annotations

from fastapi import HTTPException
from fastapi.testclient import TestClient

from server import approvals as approval_mod
from server import auth as auth_mod
from server import events as events_mod
from server.app import app
from server.events import EventStore
from veya.obase.approvals import ApprovalStore

_ORIGINAL_REQUIRE_USER = auth_mod.require_user
client = TestClient(app)


def _owner(monkeypatch):
    monkeypatch.setitem(
        app.dependency_overrides,
        _ORIGINAL_REQUIRE_USER,
        lambda: {"user_id": "owner-123", "is_owner": True},
    )


def test_needs_you_flag_off(monkeypatch):
    monkeypatch.delenv("VEYA_TDX_NEEDS_YOU", raising=False)
    _owner(monkeypatch)
    assert client.get("/api/v1/tdx/needs-you").status_code == 404


def test_needs_you_requires_signed_in_owner(monkeypatch):
    monkeypatch.setenv("VEYA_TDX_NEEDS_YOU", "true")
    monkeypatch.setenv("VEYA_OWNER_USER_ID", "owner-123")
    app.dependency_overrides.pop(_ORIGINAL_REQUIRE_USER, None)
    assert client.get("/api/v1/tdx/needs-you").status_code == 401
    monkeypatch.setitem(
        app.dependency_overrides,
        _ORIGINAL_REQUIRE_USER,
        lambda: {"user_id": "other-user", "is_owner": False},
    )
    assert client.get("/api/v1/tdx/needs-you").status_code == 403


def test_needs_you_reads_actual_approval_and_blocked_events(monkeypatch, tmp_path):
    monkeypatch.setenv("VEYA_TDX_NEEDS_YOU", "true")
    _owner(monkeypatch)
    events = EventStore(tmp_path / "events.jsonl")
    monkeypatch.setattr(events_mod, "event_store", events)
    store = ApprovalStore(tmp_path / "approvals.sqlite")
    service = approval_mod.ApprovalService(store=store)
    monkeypatch.setattr(approval_mod, "_service", service)
    scope = service.scope_for("owner-123")
    store.create_approval({
        "request_id": "real-approval-7", "scope": scope,
        "tool": "shell.exec", "tool_args": {"command": "pytest"},
        "created_at": 1000, "session_id": "session-a",
    })
    events.append({
        "topic": "task.blocked", "task_id": "real-task-7", "session_id": "session-a",
        "actor": "test", "ts": 2000,
        "payload": {"status": "blocked", "block_reason": "Waiting for review"},
    })
    events.append({
        "topic": "task.blocked", "task_id": "resolved-task", "session_id": "session-a",
        "actor": "test", "ts": 2001, "payload": {"status": "blocked"},
    })
    events.append({
        "topic": "task.completed", "task_id": "resolved-task", "session_id": "session-a",
        "actor": "test", "ts": 2002, "payload": {"status": "completed"},
    })
    events.append({"topic": "task.blocked", "session_id": "no-task-id", "actor": "test", "ts": 2003, "payload": {"status": "blocked"}})

    response = client.get("/api/v1/tdx/needs-you")
    assert response.status_code == 200
    data = response.json()
    assert data["count"] == 2
    items = {item["id"]: item for item in data["items"]}
    approval = items["approval:real-approval-7"]
    assert approval["request_id"] == "real-approval-7"
    assert approval["created_at"] == 1000
    assert approval["actions"] == ["once", "session", "always", "deny"]
    blocked = items["task:real-task-7"]
    assert blocked["title"] == "Waiting for review"
    assert blocked["created_at"] == 2000
    assert "resolved-task" not in str(data)
    assert "ev-1" not in str(data)


def test_owner_gate_reuses_tailnet_owner_decision(monkeypatch):
    monkeypatch.delenv("VEYA_OWNER_USER_ID", raising=False)
    monkeypatch.delenv("VEYA_TAILNET_OWNER_USER_ID", raising=False)
    from server.routes.tdx import require_owner

    for user in (
        {"user_id": "tester"},
        {"user_id": "owner-123"},
        {"user_id": "ordinary-user"},
    ):
        try:
            require_owner(user)
        except HTTPException as exc:
            assert exc.status_code == 403
        else:
            raise AssertionError("unconfigured non-owner user was accepted")

    assert require_owner({"user_id": "known-owner", "is_owner": True})["user_id"] == "known-owner"
    assert require_owner({"user_id": "admin-user", "role": "admin"})["user_id"] == "admin-user"
    assert require_owner({"user_id": "admin-user", "is_admin": True})["user_id"] == "admin-user"
