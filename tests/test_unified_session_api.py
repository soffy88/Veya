"""P1-02 Unified Session API tests."""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from server.events import event_store
from server.routes.sessions import (
    UnifiedSessionResumeRequest,
    attach_unified_session,
    create_unified_session,
    get_unified_session,
    get_unified_session_events,
    list_unified_sessions,
    resume_unified_session,
)
from veya.history_store import SqliteHistoryStore

_ALICE = {"user_id": "alice", "username": "alice"}
_BOB = {"user_id": "bob", "username": "bob"}


@pytest.fixture
def unified_store(tmp_path, monkeypatch):
    import veya.history_store as history_store_mod

    store = SqliteHistoryStore(tmp_path / "history.db")
    monkeypatch.setattr(history_store_mod, "default_history_store", lambda: store)
    monkeypatch.setattr(event_store, "path", tmp_path / "events.jsonl")
    return store


@pytest.mark.asyncio
async def test_unified_session_create_list_attach_and_events(unified_store):
    created = await create_unified_session(user=_ALICE)
    sid = created["session_id"]
    assert sid.startswith("sess_")

    listing = await list_unified_sessions(user=_ALICE)
    assert [item["session_id"] for item in listing["sessions"]] == [sid]

    detail = await get_unified_session(sid, user=_ALICE)
    assert detail["messages"] == []
    assert detail["active"] is False

    attached = await attach_unified_session(sid, user=_ALICE)
    assert attached["attached"] is True
    assert attached["session_id"] == sid

    events = await get_unified_session_events(sid, user=_ALICE)
    assert [event["topic"] for event in events["events"]] == ["session.created"]


@pytest.mark.asyncio
async def test_unified_session_owner_isolation(unified_store):
    created = await create_unified_session(user=_ALICE)
    sid = created["session_id"]

    listing = await list_unified_sessions(user=_BOB)
    assert listing["sessions"] == []
    with pytest.raises(HTTPException) as exc:
        await get_unified_session(sid, user=_BOB)
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_unified_resume_ready_and_execute(unified_store, monkeypatch):
    created = await create_unified_session(user=_ALICE)
    sid = created["session_id"]

    ready = await resume_unified_session(sid, user=_ALICE)
    assert ready["resumed"] is True
    assert ready["status"] == "ready"

    calls = []

    class _FakeMaster:
        async def chat_stream(self, text, *, session_id, max_rounds):
            calls.append((text, session_id, max_rounds))
            return {"status": "completed", "session_id": session_id, "final_answer": "继续完成"}

    import server.coordinator_master as coordinator_mod

    monkeypatch.setattr(coordinator_mod, "master_coordinator", _FakeMaster())
    result = await resume_unified_session(
        sid,
        UnifiedSessionResumeRequest(text="继续", max_rounds=3),
        user=_ALICE,
    )
    assert result["resumed"] is True
    assert calls == [("继续", sid, 3)]
