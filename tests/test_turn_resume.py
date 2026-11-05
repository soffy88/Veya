"""Tests for durable turn resume (Autoloop graft item 1).

Store tests use a real ApprovalStore on a temp path (CAS claim and TTL
expiry are store behavior, not mock behavior).  Turn runs use fake
runners through the module seams; no model is ever called.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from types import SimpleNamespace

import pytest

from server.bot_team.turn_resume import (
    RESUME_TTL_S,
    build_preamble,
    evaluate_resume,
    maybe_mark_done,
    maybe_resume_for_session,
    objective_hash_for,
    reap_turn_resumes,
    record_suspension,
    run_resume_for_record,
    suspension_snapshot,
)
from veya.obase.approvals import ApprovalStore


def _store(tmp_path):
    return ApprovalStore(tmp_path / "approvals.db")


def _approval(store, request_id, session_id, *, scope="scope-1", tool="bash"):
    return store.create_approval(
        {
            "request_id": request_id,
            "scope": scope,
            "tool": tool,
            "session_id": session_id,
            "created_at": time.time(),
        }
    )


def _record(store, *, task_id="task-1", session_id="sess-1", request_ids=("ap-1",), **over):
    base = {
        "bot_id": "bot-1",
        "session_id": session_id,
        "task_id": task_id,
        "trace_id": "trace-1",
        "user_id": "u1",
        "thread_id": "",
        "request_ids": list(request_ids),
        "rev": 1,
        "objective_hash": objective_hash_for("do the thing"),
    }
    base.update(over)
    return record_suspension(store=store, **base)


def _messages():
    return [{"role": "user", "text": "do the thing", "task_id": "orig"}]


class FakeConversations:
    def __init__(self, messages):
        self._messages = [dict(m) for m in messages]
        self.appended = []

    def list(self, bot_id, **kwargs):
        return [dict(m) for m in self._messages]

    def append(self, bot_id, *, role, text, task_id=None, **kwargs):
        item = {"role": role, "text": text, "task_id": task_id}
        self._messages.append(item)
        self.appended.append(item)
        return item


class FakeRunner:
    def __init__(self, reply="resumed reply", task_id="new-task", error=None):
        self.calls = []
        self.reply = reply
        self.task_id = task_id
        self.error = error

    async def __call__(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return SimpleNamespace(reply=self.reply, task_id=self.task_id)


@pytest.fixture
def seams(monkeypatch):
    runner = FakeRunner()
    monkeypatch.setattr("server.bot_team.turn_resume._RUN_TURN", runner)
    monkeypatch.setattr(
        "server.bot_team.turn_resume._RESOLVE_BOT",
        lambda bot_id: SimpleNamespace(bot_id=bot_id, name="bot"),
    )
    return runner


def test_store_claim_exactly_once_and_expiry(tmp_path):
    store = _store(tmp_path)
    row = _record(store)
    assert row["state"] == "pending"
    claimed = store.claim_turn_resume(row["resume_id"])
    assert claimed is not None and claimed["state"] == "claimed"
    assert store.claim_turn_resume(row["resume_id"]) is None
    assert store.transition_turn_resume(row["resume_id"], "done") is not None
    assert store.transition_turn_resume(row["resume_id"], "done") is None
    assert store.get_turn_resume(row["resume_id"])["state"] == "done"

    stale = _record(store, task_id="task-old")
    store.claim_turn_resume(stale["resume_id"])
    assert store.expire_overdue_turn_resumes(time.time() + RESUME_TTL_S + 10) == 1
    assert store.get_turn_resume(stale["resume_id"])["state"] == "expired"

    listed = store.list_turn_resumes(state="pending", session_id="sess-1")
    assert [r["task_id"] for r in listed] == []


def test_snapshot_hash_uses_last_user_message():
    rev, digest = suspension_snapshot(
        [
            {"role": "user", "text": "first"},
            {"role": "bot", "text": "working"},
            {"role": "user", "text": "do the thing"},
        ]
    )
    assert rev == 3
    assert digest == objective_hash_for("do the thing")
    assert suspension_snapshot([]) == (0, objective_hash_for(""))


def test_evaluate_waiting_denied_changed_ready(tmp_path):
    store = _store(tmp_path)
    record = _record(store)
    pending = [_approval(store, "ap-1", "sess-1")]
    assert evaluate_resume(record, approvals=pending, messages=_messages())[0] == "skip"

    store.resolve_approval("ap-1", decision="deny")
    resolved = store.list_approvals(session_id="sess-1")
    verdict, reason, _ = evaluate_resume(record, approvals=resolved, messages=_messages())
    assert (verdict, reason) == ("done", "denied")

    store2 = _store(tmp_path / "sub")
    record2 = _record(store2)
    _approval(store2, "ap-1", "sess-1")
    store2.resolve_approval("ap-1", decision="allow")
    resolved2 = store2.list_approvals(session_id="sess-1")
    changed = [{"role": "user", "text": "do something else"}]
    verdict, reason, _ = evaluate_resume(record2, approvals=resolved2, messages=changed)
    assert (verdict, reason) == ("done", "objective-changed")
    verdict, reason, outcomes = evaluate_resume(
        record2, approvals=resolved2, messages=_messages()
    )
    assert (verdict, reason) == ("resume", "ready")
    assert outcomes == [
        {
            "request_id": "ap-1",
            "tool": "bash",
            "decision": "allow",
            "status": "resolved",
            "expired": False,
        }
    ]


async def test_run_resume_ready_runs_new_turn(tmp_path, seams):
    store = _store(tmp_path)
    record = _record(store)
    _approval(store, "ap-1", "sess-1")
    store.resolve_approval("ap-1", decision="allow")
    convos = FakeConversations(_messages())
    outcome = await run_resume_for_record(record, store=store, conversations=convos)
    assert outcome == "resumed"
    assert len(seams.calls) == 1
    call = seams.calls[0]
    assert call["user"] == {"user_id": "u1"}
    assert "task-1" in call["text"] and "allow" in call["text"]
    assert store.get_turn_resume(record["resume_id"])["state"] == "done"


async def test_run_resume_denied_writes_note_no_turn(tmp_path, seams):
    store = _store(tmp_path)
    record = _record(store)
    _approval(store, "ap-1", "sess-1")
    store.resolve_approval("ap-1", decision="deny")
    convos = FakeConversations(_messages())
    outcome = await run_resume_for_record(record, store=store, conversations=convos)
    assert outcome == "done-denied"
    assert seams.calls == []
    assert any("已否决，未继续" in m["text"] for m in convos.appended)
    assert store.get_turn_resume(record["resume_id"])["state"] == "done"


async def test_run_resume_already_claimed_runs_once(tmp_path, seams):
    store = _store(tmp_path)
    record = _record(store)
    _approval(store, "ap-1", "sess-1")
    store.resolve_approval("ap-1", decision="allow")
    assert store.claim_turn_resume(record["resume_id"]) is not None
    convos = FakeConversations(_messages())
    outcome = await run_resume_for_record(record, store=store, conversations=convos)
    assert outcome == "already-claimed"
    assert seams.calls == []


async def test_run_resume_runner_crash_is_terminal_and_visible(tmp_path, seams):
    store = _store(tmp_path)
    record = _record(store)
    _approval(store, "ap-1", "sess-1")
    store.resolve_approval("ap-1", decision="allow")
    seams.error = RuntimeError("boom")
    convos = FakeConversations(_messages())
    outcome = await run_resume_for_record(record, store=store, conversations=convos)
    assert outcome == "failed"
    assert any("续行失败" in m["text"] for m in convos.appended)
    assert store.get_turn_resume(record["resume_id"])["state"] == "done"


async def test_run_resume_group_thread_appends_room_reply(tmp_path, seams, monkeypatch):
    store = _store(tmp_path)
    record = _record(store, thread_id="grp-1")
    _approval(store, "ap-1", "sess-1")
    store.resolve_approval("ap-1", decision="allow")
    room_calls = []

    class FakeGroupStore:
        def append_message(self, group_id, *, role, text, bot_id=None, task_id=None, **kwargs):
            room_calls.append(
                {"group_id": group_id, "role": role, "text": text, "bot_id": bot_id, "task_id": task_id}
            )

    monkeypatch.setattr("server.bot_team.groups.GroupStore", FakeGroupStore)
    convos = FakeConversations(_messages())
    outcome = await run_resume_for_record(record, store=store, conversations=convos)
    assert outcome == "resumed"
    assert room_calls == [
        {"group_id": "grp-1", "role": "bot", "text": "resumed reply", "bot_id": "bot-1", "task_id": "new-task"}
    ]


def test_maybe_mark_done_only_when_all_resolved(tmp_path):
    store = _store(tmp_path)
    record = _record(store)
    _approval(store, "ap-1", "sess-1")
    assert maybe_mark_done(record["resume_id"], store=store, request_ids=["ap-1"], session_id="sess-1") is False
    store.resolve_approval("ap-1", decision="allow")
    assert maybe_mark_done(record["resume_id"], store=store, request_ids=["ap-1"], session_id="sess-1") is True
    assert store.get_turn_resume(record["resume_id"])["state"] == "done"


async def test_reap_expires_and_resumes(tmp_path, seams):
    store = _store(tmp_path)
    ready = _record(store, task_id="task-ready")
    waiting = _record(store, task_id="task-waiting", session_id="sess-2", request_ids=("ap-2",))
    _approval(store, "ap-1", "sess-1")
    _approval(store, "ap-2", "sess-2")
    store.resolve_approval("ap-1", decision="allow")
    convos = FakeConversations(_messages())
    counts = await reap_turn_resumes(store=store, conversations=convos)
    assert counts["resumed"] == 1
    assert counts["waiting"] == 1
    assert store.get_turn_resume(ready["resume_id"])["state"] == "done"
    assert store.get_turn_resume(waiting["resume_id"])["state"] == "pending"


async def test_session_sweep_skips_live_turn(tmp_path, seams, monkeypatch):
    from server.bot_team import turns as turns_mod

    store = _store(tmp_path)
    record = _record(store)
    _approval(store, "ap-1", "sess-1")
    store.resolve_approval("ap-1", decision="allow")
    monkeypatch.setitem(turns_mod._AWAITING_TASKS, "task-1", object())
    convos = FakeConversations(_messages())
    outcomes = await maybe_resume_for_session("sess-1", store=store, conversations=convos)
    assert outcomes == ["skipped-live"]
    assert seams.calls == []
    assert store.get_turn_resume(record["resume_id"])["state"] == "pending"


async def test_finish_or_await_approval_records_suspension(tmp_path, monkeypatch):
    from server.approvals import ApprovalService
    from server.bot_team.turns import (
        _AWAITING_TASKS,
        APPROVAL_WAIT_REPLY,
        BotTurnStart,
        _finish_or_await_approval,
    )
    from server.goal_run.bot_identity import BotSpec

    store = _store(tmp_path)
    service = ApprovalService(store=store)
    monkeypatch.setattr("server.approvals.get_approval_service", lambda: service)
    monkeypatch.setattr("veya.obase.approvals.ApprovalStore", lambda path=None: store)

    scope = ApprovalService.scope_for("u1")

    async def slow_finishing():
        store.create_approval(
            {
                "request_id": "ap-1",
                "scope": scope,
                "tool": "bash",
                "session_id": "sess-1",
                "created_at": time.time(),
            }
        )
        await asyncio.sleep(30)

    convos = FakeConversations(_messages())
    start = BotTurnStart(bot_id="bot-1", task_id="task-1", session_id="sess-1", trace_id="trace-1")
    try:
        result = await asyncio.wait_for(
            _finish_or_await_approval(
                slow_finishing(),
                bot=BotSpec(bot_id="bot-1"),
                start=start,
                user={"user_id": "u1"},
                conversations=convos,
            ),
            timeout=5,
        )
        assert "task-1" in _AWAITING_TASKS
    finally:
        task = _AWAITING_TASKS.get("task-1")
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
    assert result.status == "awaiting_approval"
    assert result.reply == APPROVAL_WAIT_REPLY
    rows = store.list_turn_resumes(state="pending", session_id="sess-1")
    assert len(rows) == 1
    row = rows[0]
    assert row["task_id"] == "task-1"
    assert row["request_ids"] == ["ap-1"]
    assert row["rev"] == 1
    assert row["objective_hash"] == objective_hash_for("do the thing")
    assert row["user_id"] == "u1"
    assert any(m["text"] == APPROVAL_WAIT_REPLY for m in convos.appended)


def test_preamble_names_outcomes_and_variant():
    record = {"task_id": "task-1"}
    outcomes = [{"tool": "bash", "request_id": "ap-1", "decision": "allow"}]
    text = build_preamble(record, outcomes, ended_before=True)
    assert "task-1" in text and "allow" in text and "超时" in text
    text2 = build_preamble(record, outcomes, ended_before=False)
    assert "重启" in text2
