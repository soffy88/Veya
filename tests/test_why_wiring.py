"""Wiring tests: why route + turn_why tool (Autoloop graft item 2).

Route tests use a bare FastAPI app with only the bot_team router and the
auth dependency overridden (same pattern as tests/test_allon_features.py),
so no production store is touched: every store explain_turn reads is
patched to a fake.
"""

from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server import auth as auth_mod


class FakeEventStore:
    def __init__(self, events):
        self._events = list(events)

    def read_all(self, task_id=None, **kwargs):
        return [e for e in self._events if e.get("task_id") == task_id]


class FakeApprovalStore:
    def __init__(self, rows):
        self._rows = list(rows)

    def list_approvals(self, session_id="", limit=200, **kwargs):
        return [r for r in self._rows if r.get("session_id") == session_id][:limit]


class FakeLedgerModule:
    def __init__(self, rows):
        self._rows = list(rows)

    def export_ledger(self, limit=None):
        return self._rows[:limit] if limit else list(self._rows)

    def trace_decision_chain(self, decision_id, limit=20):
        return [{"id": decision_id, "parent_id": None}]


class FakeAttentionStore:
    def __init__(self, items):
        self._items = list(items)

    def list(self, **kwargs):
        return list(self._items)


def _patch_stores(monkeypatch, events=(), approvals=(), ledger_rows=(), attention=()):
    monkeypatch.setattr("server.events.EventStore", lambda: FakeEventStore(events))
    monkeypatch.setattr("veya.obase.approvals.ApprovalStore", lambda: FakeApprovalStore(approvals))
    fake_ledger = FakeLedgerModule(ledger_rows)
    monkeypatch.setattr("server.decision_ledger.export_ledger", fake_ledger.export_ledger)
    monkeypatch.setattr(
        "server.decision_ledger.trace_decision_chain", fake_ledger.trace_decision_chain
    )
    monkeypatch.setattr(
        "server.bot_team.attention.AttentionStore", lambda: FakeAttentionStore(attention)
    )


@pytest.fixture
def owner_client(monkeypatch: pytest.MonkeyPatch):
    user = {"user_id": "owner-uid", "username": "owner", "is_owner": True}

    def _owner():
        return dict(user)

    real_require_user = auth_mod.require_user
    real_get_current_user = auth_mod.get_current_user
    app = FastAPI()
    from server.routes import bot_team

    app.include_router(bot_team.router)
    app.dependency_overrides[real_require_user] = _owner
    app.dependency_overrides[real_get_current_user] = _owner
    monkeypatch.setattr(auth_mod, "require_user", _owner)
    monkeypatch.setattr(auth_mod, "get_current_user", _owner)
    return TestClient(app)


def test_why_route_unknown_task_returns_gaps(owner_client, monkeypatch):
    _patch_stores(monkeypatch)
    resp = owner_client.get("/api/v1/bots/turns/no-such-task/why")
    assert resp.status_code == 200
    body = resp.json()
    assert body["task_id"] == "no-such-task"
    assert body["timeline"] == [] and body["outcome"] == "无记录"
    assert "timeline" in body["gaps"]


def test_why_route_returns_timeline(owner_client, monkeypatch):
    events = [
        {
            "event_id": "ev-1",
            "task_id": "task-1",
            "session_id": "sess-1",
            "trace_id": "trace-1",
            "topic": "tool.requested",
            "ts": 1000.0,
            "actor": "bot",
            "payload": {"summary": "ran"},
        }
    ]
    _patch_stores(monkeypatch, events=events)
    resp = owner_client.get("/api/v1/bots/turns/task-1/why")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["timeline"]) == 1
    assert body["timeline"][0]["topic"] == "tool.requested"
    assert body["outcome"] == "tool.requested"
    assert body["session_id"] == "sess-1"


async def test_turn_why_tool_registered_and_delegates(monkeypatch):
    from server.tool_registry import master_tools

    names = [s.get("function", {}).get("name") for s in master_tools._schemas]
    assert "turn_why" in names
    fn = master_tools._functions.get("turn_why")
    assert fn is not None

    captured: dict = {}

    def fake_explain(task_id, *, session_id=None, event_limit=500):
        captured.update(task_id=task_id, session_id=session_id, event_limit=event_limit)
        return {"task_id": task_id, "gaps": []}

    monkeypatch.setattr("server.bot_team.why.explain_turn", fake_explain)
    out = await fn(task_id="task-9", session_id="sess-9", limit=3)
    assert json.loads(out)["task_id"] == "task-9"
    assert captured == {"task_id": "task-9", "session_id": "sess-9", "event_limit": 3}
