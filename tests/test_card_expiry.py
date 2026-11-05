"""Card expiry (Autoloop 1.1.1 graft): approvals + attention + turn resume.

Approvals live 7 days (APPROVAL_TTL_S); overdue cards can no longer resolve
(store CAS + service 409), are hidden from pending lists, and are swept to
``expired`` at startup. Attention questions live 7 days too: expired items
stay visible but can no longer be answered (dismissal stays allowed). A
suspended turn whose approval expired ends with a visible note, never a
silent wait.
"""

from __future__ import annotations

import json
import sqlite3
import time

import pytest
from fastapi import HTTPException

from veya.obase.approvals import APPROVAL_TTL_S, ApprovalStore

pytestmark = pytest.mark.asyncio

NOW = time.time()


def _store(tmp_path):
    return ApprovalStore(str(tmp_path / "approvals.db"))


def _approval(store, *, request_id="req-1", created_at=None, **over):
    record = {
        "request_id": request_id,
        "scope": "scope-1",
        "tool": "delete_file",
        "tool_args": {"path": "/tmp/x"},
        "session_id": "sess-1",
        "actor": "master",
    }
    if created_at is not None:
        record["created_at"] = created_at
    record.update(over)
    return store.create_approval(record)


# ── approvals: store ────────────────────────────────────────────────


def test_create_sets_expiry(tmp_path):
    store = _store(tmp_path)
    row = _approval(store, created_at=NOW)
    assert row["status"] == "pending"
    assert row["expires_at"] == NOW + APPROVAL_TTL_S
    assert row["expired"] is False


def test_resolve_refuses_overdue(tmp_path):
    store = _store(tmp_path)
    _approval(store, created_at=NOW - 8 * 24 * 3600)
    assert store.resolve_approval("req-1", decision="once") is None
    row = store.get_approval("req-1")
    assert row is not None and row["status"] == "pending" and row["expired"] is True


def test_resolve_fresh_works(tmp_path):
    store = _store(tmp_path)
    _approval(store)
    row = store.resolve_approval("req-1", decision="once")
    assert row is not None and row["status"] == "resolved" and row["decision"] == "once"


def test_list_hides_overdue_by_default(tmp_path):
    store = _store(tmp_path)
    _approval(store, request_id="fresh")
    _approval(store, request_id="stale", created_at=NOW - 8 * 24 * 3600)
    assert [r["request_id"] for r in store.list_approvals()] == ["fresh"]
    assert {r["request_id"] for r in store.list_approvals(include_expired=True)} == {
        "fresh",
        "stale",
    }
    assert store.list_approvals(status="pending") == store.list_approvals()
    # Unswept overdue rows show under status="expired" too.
    assert [r["request_id"] for r in store.list_approvals(status="expired")] == ["stale"]


def test_sweep_marks_overdue(tmp_path):
    store = _store(tmp_path)
    _approval(store, request_id="stale", created_at=NOW - 8 * 24 * 3600)
    _approval(store, request_id="fresh")
    assert store.expire_overdue_approvals(now=NOW) == 1
    assert store.get_approval("stale")["status"] == "expired"
    assert store.get_approval("fresh")["status"] == "pending"
    assert store.expire_overdue_approvals(now=NOW) == 0


def test_migration_backfills_old_db(tmp_path):
    path = tmp_path / "approvals.db"
    conn = sqlite3.connect(str(path))
    conn.execute(
        """CREATE TABLE approvals (
            request_id TEXT PRIMARY KEY, scope TEXT NOT NULL, tool TEXT NOT NULL,
            tool_args_json TEXT NOT NULL DEFAULT '{}', status TEXT NOT NULL,
            decision TEXT, decision_reason TEXT, delegate_policy TEXT,
            session_id TEXT, actor TEXT, created_at REAL NOT NULL,
            updated_at REAL NOT NULL, resolved_at REAL
        )"""
    )
    conn.execute(
        "INSERT INTO approvals (request_id, scope, tool, status, created_at, updated_at)"
        " VALUES ('old-1', 's', 't', 'pending', ?, ?)",
        (NOW - 30 * 24 * 3600, NOW - 30 * 24 * 3600),
    )
    conn.commit()
    conn.close()
    store = ApprovalStore(str(path))
    row = store.get_approval("old-1")
    assert row is not None
    assert row["expires_at"] == NOW - 30 * 24 * 3600 + APPROVAL_TTL_S
    assert store.expire_overdue_approvals(now=NOW) == 1
    assert store.get_approval("old-1")["status"] == "expired"


def test_grandfathered_null_row_still_resolves(tmp_path):
    store = _store(tmp_path)
    _approval(store)
    conn = sqlite3.connect(str(tmp_path / "approvals.db"))
    conn.execute("UPDATE approvals SET expires_at=NULL WHERE request_id='req-1'")
    conn.commit()
    conn.close()
    row = store.get_approval("req-1")
    assert row is not None and row["expires_at"] is None and row["expired"] is False
    assert [r["request_id"] for r in store.list_approvals()] == ["req-1"]
    resolved = store.resolve_approval("req-1", decision="once")
    assert resolved is not None and resolved["status"] == "resolved"


# ── approvals: service ──────────────────────────────────────────────


@pytest.fixture()
def wired(tmp_path, monkeypatch):
    from server import approvals as approvals_mod

    monkeypatch.setenv("VEYA_APPROVAL_GATES", "1")
    store = ApprovalStore(str(tmp_path / "approvals.db"))
    approvals_mod.configure_service(store=store)
    approvals_mod.session_grants.clear()
    yield store
    approvals_mod.session_grants.clear()


async def test_service_resolve_expired_409(wired):
    from server import approvals as approvals_mod

    _approval(wired, created_at=time.time() - 8 * 24 * 3600)
    with pytest.raises(HTTPException) as excinfo:
        await approvals_mod.get_approval_service().resolve(
            approval_id="req-1", decision="once", principal="human"
        )
    assert excinfo.value.status_code == 409
    assert "expired" in str(excinfo.value.detail)
    row = wired.get_approval("req-1")
    assert row is not None and row["status"] == "pending"


# ── attention ───────────────────────────────────────────────────────


def _att_store(tmp_path):
    from server.bot_team.attention import AttentionStore

    return AttentionStore(tmp_path / "bots-root")


def _expire_item_file(store, item_id):
    data = json.loads(store.path.read_text(encoding="utf-8"))
    for item in data:
        if item["id"] == item_id:
            item["expires_at"] = time.time() - 10
    store.path.write_text(json.dumps(data), encoding="utf-8")


def test_attention_create_sets_expiry(tmp_path):
    from server.bot_team.attention import ATTENTION_TTL_S

    store = _att_store(tmp_path)
    item = store.create(question="Which hotel?", bot_id="alpha")
    assert item["status"] == "open"
    assert item["expires_at"] == pytest.approx(item["created_ts"] + ATTENTION_TTL_S, abs=2)
    assert item["expired"] is False


def test_attention_answer_refuses_expired_dismiss_works(tmp_path):
    store = _att_store(tmp_path)
    item = store.create(question="Which hotel?", bot_id="alpha")
    _expire_item_file(store, item["id"])
    assert store.answer(item["id"], "The Grand.") is None
    assert store.get(item["id"])["status"] == "open"
    closed = store.dismiss(item["id"])
    assert closed is not None and closed["status"] == "dismissed"


def test_attention_open_list_skips_expired(tmp_path):
    store = _att_store(tmp_path)
    fresh = store.create(question="Fresh?", bot_id="alpha")
    stale = store.create(question="Stale?", bot_id="alpha")
    _expire_item_file(store, stale["id"])
    assert store.open_count() == 1
    assert [i["id"] for i in store.list(status="open")] == [fresh["id"]]
    all_items = store.list()
    assert len(all_items) == 2
    by_id = {i["id"]: i for i in all_items}
    assert by_id[stale["id"]]["expired"] is True
    assert by_id[fresh["id"]]["expired"] is False


# ── turn resume ─────────────────────────────────────────────────────


def test_evaluate_resume_expired():
    from server.bot_team.turn_resume import evaluate_resume

    record = {"request_ids": ["r1"], "rev": 0, "objective_hash": "x"}
    approvals = [
        {
            "request_id": "r1",
            "tool": "delete_file",
            "decision": None,
            "status": "pending",
            "expires_at": time.time() - 10,
        }
    ]
    verdict, reason, outcomes = evaluate_resume(record, approvals=approvals, messages=[])
    assert (verdict, reason) == ("done", "expired")
    assert outcomes[0]["expired"] is True


def test_evaluate_resume_grandfathered_null_waits():
    from server.bot_team.turn_resume import evaluate_resume

    record = {"request_ids": ["r1"], "rev": 0, "objective_hash": "x"}
    approvals = [
        {
            "request_id": "r1",
            "tool": "delete_file",
            "decision": None,
            "status": "pending",
            "expires_at": None,
        }
    ]
    verdict, reason, _ = evaluate_resume(record, approvals=approvals, messages=[])
    assert (verdict, reason) == ("skip", "waiting")


class _FakeConversations:
    def __init__(self):
        self.notes = []

    def list(self, bot_id):
        return []

    def append(self, bot_id, *, role, text, task_id=""):
        self.notes.append(text)


async def test_run_resume_expired_note(tmp_path):
    from server.bot_team.turn_resume import run_resume_for_record

    store = _store(tmp_path)
    _approval(
        store,
        request_id="r1",
        session_id="sess-9",
        created_at=time.time() - 8 * 24 * 3600,
    )
    record = store.create_turn_resume(
        {
            "resume_id": "res-test1",
            "bot_id": "bot-1",
            "session_id": "sess-9",
            "task_id": "task-1",
            "request_ids": ["r1"],
            "rev": 0,
            "objective_hash": "",
            "created_at": time.time(),
            "expires_at": time.time() + APPROVAL_TTL_S,
        }
    )
    fake = _FakeConversations()
    result = await run_resume_for_record(record, store=store, conversations=fake)
    assert result == "done-expired"
    assert any("已过期" in note for note in fake.notes)
    assert store.get_turn_resume("res-test1")["state"] == "done"
