"""Tests for server.bot_team.why (Autoloop graft item 2, read-only)."""

from __future__ import annotations

from server.bot_team.why import explain_turn


def _event(task_id, ts, topic, session_id="sess-1", trace_id="trace-1", payload=None):
    return {
        "event_id": f"ev-{ts}",
        "task_id": task_id,
        "session_id": session_id,
        "trace_id": trace_id,
        "topic": topic,
        "ts": float(ts),
        "actor": "bot",
        "payload": payload or {},
    }


class FakeEventStore:
    def __init__(self, events):
        self._events = list(events)

    def read_all(self, task_id=None, **kwargs):
        return [e for e in self._events if e.get("task_id") == task_id]


class FakeApprovalStore:
    def __init__(self, rows):
        self._rows = list(rows)

    def list_approvals(self, session_id="", limit=100, **kwargs):
        return [r for r in self._rows if r.get("session_id") == session_id][:limit]


class FakeLedger:
    def __init__(self, rows):
        self._rows = list(rows)

    def export_ledger(self, limit=None):
        return self._rows[:limit] if limit else list(self._rows)

    def trace_decision_chain(self, decision_id, limit=20):
        return [{"id": decision_id, "parent_id": None}]


class FakeAttention:
    def __init__(self, items):
        self._items = list(items)

    def list(self, **kwargs):
        return list(self._items)


def _explain(events=(), approvals=(), ledger_rows=(), attention=(), **kwargs):
    return explain_turn(
        "task-1",
        event_store=FakeEventStore(events),
        approval_store=FakeApprovalStore(approvals),
        ledger=FakeLedger(ledger_rows),
        attention_store=FakeAttention(attention),
        **kwargs,
    )


def test_empty_turn_reports_gaps_instead_of_inferring():
    report = _explain()
    assert report["task_id"] == "task-1"
    assert report["timeline"] == []
    assert report["outcome"] == "无记录"
    assert report["rule_version"] == "无"
    assert report["approvals"] == []
    assert report["decision_records"] == []
    assert report["attention"] == []
    for section in (
        "timeline",
        "outcome",
        "rule_version",
        "approvals",
        "decision_records",
        "attention",
    ):
        assert section in report["gaps"]


def test_timeline_exact_match_sorted_and_capped():
    events = [
        _event("task-1", 3, "tool.failed"),
        _event("other", 9, "tool.requested"),
        _event("task-1", 1, "turn.started"),
        _event("task-1", 2, "tool.requested"),
    ]
    report = _explain(events, event_limit=2)
    assert [e["topic"] for e in report["timeline"]] == ["tool.requested", "tool.failed"]
    assert report["timeline_truncated"] is True
    assert report["timeline_limit"] == 2
    assert report["started_at"] is not None and report["ended_at"] is not None
    assert report["outcome"] == "tool.failed"
    assert report["trace_ids"] == ["trace-1"]
    assert report["session_id"] == "sess-1"


def test_payload_denylist_and_string_cap():
    long_text = "x" * 400
    events = [
        _event(
            "task-1",
            1,
            "tool.requested",
            payload={"summary": "ok", "args": {"secret": 1}, "token": "abc", "detail": long_text},
        )
    ]
    report = _explain(events)
    payload = report["timeline"][0]["payload"]
    assert payload["summary"] == "ok"
    assert "args" not in payload and "token" not in payload
    assert payload["detail"].endswith("…") and len(payload["detail"]) == 301


def test_rule_version_prefers_standing_over_sentinel():
    events = [
        _event("task-1", 1, "sentinel.decision", payload={"settled_by": "allow"}),
        _event("task-1", 2, "standing.refused", payload={"standing_id": "st-7"}),
    ]
    report = _explain(events)
    assert report["rule_version"] == "standing"
    assert report["rule_evidence"][0]["standing_id"] == "st-7"


def test_approvals_decisions_attention_join():
    events = [_event("task-1", 1, "turn.started")]
    approvals = [
        {
            "request_id": "ap-1",
            "tool": "bash",
            "status": "resolved",
            "decision": "allow",
            "session_id": "sess-1",
            "created_at": 1,
            "resolved_at": 2,
        },
        {
            "request_id": "ap-2",
            "tool": "bash",
            "status": "pending",
            "decision": None,
            "session_id": "other",
            "created_at": 1,
            "resolved_at": None,
        },
    ]
    ledger_rows = [
        {"id": "d-1", "category": "spend", "scenario": "task-1 买了东西", "outcome": "done"},
        {"id": "d-2", "category": "other", "scenario": "无关", "outcome": "done"},
    ]
    attention = [
        {
            "id": "att-1",
            "question": "批吗",
            "context": "sess-1 的事",
            "status": "open",
            "answer": None,
            "standing_id": None,
            "created_ts": 1,
        },
    ]
    report = _explain(events, approvals, ledger_rows, attention)
    assert [a["request_id"] for a in report["approvals"]] == ["ap-1"]
    assert [d["id"] for d in report["decision_records"]] == ["d-1"]
    assert report["decision_records"][0]["chain"] == [{"id": "d-1", "parent_id": None}]
    assert [a["id"] for a in report["attention"]] == ["att-1"]
    # No standing/sentinel evidence in this turn: only the rule gap remains.
    assert report["gaps"] == ["rule_version"]


def test_no_session_means_no_approvals_section():
    events = [_event("task-1", 1, "turn.started", session_id="")]
    report = _explain(events, session_id="")
    assert report["session_id"] in (None, "")
    assert report["approvals"] == []
    assert "approvals" in report["gaps"]
