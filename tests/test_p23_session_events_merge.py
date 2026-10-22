"""P3-2: `round_compaction` must be visible through the session events API.

The compaction event is written by the ReAct loop through ``fire_step`` into
the durable per-session journal (Postgres/SQLite ``session_events``), while
``GET /api/v1/sessions/{sid}/events`` used to read only the local JSONL
``EventStore``. Compaction therefore looked like it never ran.
"""

from __future__ import annotations

import uuid

import pytest

from server.events import event_store
from server.routes.sessions import get_unified_session_events
from server.session_events import durable_session_store
from veya.history_store import SqliteHistoryStore

_ALICE = {"user_id": "alice", "username": "alice"}


@pytest.fixture
def isolated_stores(tmp_path, monkeypatch):
    import veya.history_store as history_store_mod

    store = SqliteHistoryStore(tmp_path / "history.db")
    monkeypatch.setattr(history_store_mod, "default_history_store", lambda: store)
    monkeypatch.setattr(event_store, "path", tmp_path / "events.jsonl")
    durable_session_store.reset_transient_state()
    yield store
    durable_session_store.reset_transient_state()


def _register_session(store: SqliteHistoryStore, sid: str) -> None:
    """Make the session visible to the ownership check in the route."""
    store.save_sync(sid, [{"role": "user", "content": "hi"}], user_id=_ALICE["user_id"])


@pytest.mark.asyncio
async def test_round_compaction_event_is_returned_by_session_events_api(isolated_stores):
    sid = f"sess_p32_{uuid.uuid4().hex[:10]}"
    _register_session(isolated_stores, sid)

    # The local JSONL store carries a lifecycle-style event, as in production.
    event_store.append(
        {
            "topic": "session.created",
            "session_id": sid,
            "trace_id": sid,
            "actor": "alice",
            "payload": {},
        }
    )
    # The ReAct loop publishes compaction through the durable journal.
    await durable_session_store.append_event(
        sid,
        "round_compaction",
        {
            "type": "round_compaction",
            "session_id": sid,
            "rounds": 7,
            "compacted_messages": 5,
        },
    )

    payload = await get_unified_session_events(sid, user=_ALICE)
    events = payload["events"]
    topics = [event.get("topic") for event in events]

    assert "round_compaction" in topics
    compaction = next(e for e in events if e.get("topic") == "round_compaction")
    assert compaction["payload"]["rounds"] == 7
    assert compaction["payload"]["compacted_messages"] == 5
    assert compaction["source"] == "session_events"

    # The pre-existing JSONL source is still returned and labelled.
    created = next(e for e in events if e.get("topic") == "session.created")
    assert created["source"] == "event_store"


@pytest.mark.asyncio
async def test_events_api_spans_multiple_epochs(isolated_stores):
    """A second turn starts a new epoch; both turns stay readable."""
    sid = f"sess_p32b_{uuid.uuid4().hex[:10]}"
    _register_session(isolated_stores, sid)

    await durable_session_store.begin_stream(sid)
    await durable_session_store.append_event(sid, "round_compaction", {"rounds": 7})
    await durable_session_store.begin_stream(sid)
    await durable_session_store.append_event(sid, "round_compaction", {"rounds": 13})

    payload = await get_unified_session_events(sid, user=_ALICE)
    compactions = [e for e in payload["events"] if e.get("topic") == "round_compaction"]

    assert [e["epoch"] for e in compactions] == [1, 2]
    assert [e["payload"]["rounds"] for e in compactions] == [7, 13]
