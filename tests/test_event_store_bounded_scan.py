"""EventStore must not decode the whole shared log on the hot path (2026-10-12).

~/.veya/events.jsonl reached 670 MB on ubuntu26; every remote execution paid a
full JSON decode of it, synchronously on the asyncio loop.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from server.events import EventStore, event_store
from server.task_store import TaskStore


def _write(path: Path, events: list[dict], *, ascii_only: bool = False) -> None:
    with path.open("a", encoding="utf-8") as handle:
        for event in events:
            handle.write(json.dumps(event, ensure_ascii=ascii_only) + "\n")


def test_tests_never_touch_the_real_user_event_log() -> None:
    assert Path.home() / ".veya" not in event_store.path.parents


def test_append_with_a_fresh_id_never_scans_the_log(tmp_path: Path, monkeypatch) -> None:
    store = EventStore(tmp_path / "events.jsonl")
    _write(store.path, [{"event_id": "old", "topic": "x"}])

    def boom() -> set[str]:
        raise AssertionError("full-log scan on the hot path")

    monkeypatch.setattr(store, "_load_known_event_ids", boom)
    store.append({"topic": "task.created", "session_id": "s", "payload": {}})
    store.append({"topic": "task.created", "session_id": "s", "payload": {}})
    assert len(store.path.read_text(encoding="utf-8").splitlines()) == 3


def test_a_caller_supplied_id_stays_idempotent_across_the_existing_log(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    _write(path, [{"event_id": "on-disk", "topic": "x"}, {"event_id": "esc\u00e9", "topic": "y"}])
    store = EventStore(path)
    store.append({"event_id": "on-disk", "topic": "x"})
    store.append({"event_id": "new-1", "topic": "x"})
    store.append({"event_id": "new-1", "topic": "x"})
    ids = [json.loads(line)["event_id"] for line in path.read_text(encoding="utf-8").splitlines()]
    assert ids == ["on-disk", "esc\u00e9", "new-1"]


EVENTS = [
    {"event_id": "1", "topic": "task.created", "task_id": "t1", "session_id": "s1"},
    {"event_id": "2", "topic": "tool.approved", "task_id": "t1", "session_id": "s2"},
    {"event_id": "3", "type": "task.updated", "task_id": "t2", "session_id": "s1"},
    {
        "event_id": "4",
        "topic": "other",
        "task_id": "t1",
        "session_id": "s1",
        "payload": {"note": "mentions task.created and t1 in text"},
    },
    {"event_id": "5", "topic": "task.done", "task_id": "t\u00fc", "session_id": "s\u00fc"},
    {"event_id": "6", "topic": "task.created", "task_id": "t10", "session_id": "s10"},
]


def _naive(events, *, task_id=None, session_id=None, topics=None, topic_prefixes=None):
    out = []
    for e in events:
        if task_id is not None and e.get("task_id") != task_id:
            continue
        if session_id is not None and e.get("session_id") != session_id:
            continue
        if topics is not None or topic_prefixes is not None:
            loose = str(e.get("topic") or e.get("type") or e.get("event") or "")
            ok = (topics is not None and e.get("topic") in topics) or (
                topic_prefixes is not None and loose.startswith(topic_prefixes)
            )
            if not ok:
                continue
        out.append(e)
    return out


@pytest.mark.parametrize("ascii_only", [False, True])
@pytest.mark.parametrize(
    "filters",
    [
        {},
        {"task_id": "t1"},
        {"task_id": "t\u00fc"},
        {"session_id": "s1"},
        {"session_id": "s\u00fc"},
        {"topics": {"task.created"}},
        {"topics": {"tool.approved"}, "topic_prefixes": ("task.",)},
        {"task_id": "t1", "topics": {"task.created"}},
        {"task_id": "t1", "session_id": "s1"},
    ],
)
def test_filtered_read_matches_a_full_decode(tmp_path: Path, filters, ascii_only) -> None:
    store = EventStore(tmp_path / "events.jsonl")
    _write(store.path, EVENTS, ascii_only=ascii_only)
    with store.path.open("a", encoding="utf-8") as handle:
        handle.write("not json at all task.created t1\n\n")
    assert store.read_all(**filters) == _naive(EVENTS, **filters)


def test_task_store_rebuilds_from_task_events_only(tmp_path: Path) -> None:
    events = tmp_path / "events.jsonl"
    first = TaskStore(tmp_path / "tasks.json", event_path=events)
    task = first.create(session_id="s", title="t", objective="o")
    _write(events, [{"event_id": f"n{i}", "topic": "noise", "payload": {}} for i in range(50)])
    second = TaskStore(tmp_path / "tasks2.json", event_path=events)
    assert second.get(task.id) is not None
