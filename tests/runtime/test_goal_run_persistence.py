"""P3 GoalRun persistence: durable execution authority over restarts.

Covers the P3 acceptance gate: create a goal (id/goal/status/timestamps),
drive it through admitted -> running -> completed, restart the repository
(a new instance over the same SQLite file, the crash analogue), and prove
get_goal_run(id) restores state with events preserved. Illegal moves fail.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from runtime.execution.durable import DurableExecutionError, DurableExecutionRepository


async def _repo(path: Path) -> DurableExecutionRepository:
    repo = DurableExecutionRepository(sqlite_path=path)
    await repo.connect()
    return repo


async def test_create_goal_run_persists_identity(tmp_path: Path) -> None:
    repo = await _repo(tmp_path / "p3.sqlite3")
    row = await repo.create_goal_run(goal="Add a utility function and tests.")
    assert row["id"]
    assert row["status"] == "created"
    assert row["goal_text"] == "Add a utility function and tests."
    assert row["created_at"] and row["updated_at"]

    fetched = await repo.get_goal_run(row["id"])
    assert fetched is not None
    assert fetched["id"] == row["id"]
    assert fetched["goal_text"] == "Add a utility function and tests."
    assert fetched["status"] == "created"

    assert await repo.get_goal_run("does-not-exist") is None


async def test_transition_lifecycle_and_events(tmp_path: Path) -> None:
    repo = await _repo(tmp_path / "p3.sqlite3")
    row = await repo.create_goal_run(goal="Ship it.")
    gid = row["id"]

    admitted = await repo.transition_goal_run(gid, "admitted", reason="pre-admission accepted")
    assert admitted["status"] == "admitted"
    running = await repo.transition_goal_run(gid, "running", reason="worker launched")
    assert running["status"] == "running"
    # Converged replay of the same state is idempotent, not an error.
    again = await repo.transition_goal_run(gid, "running", reason="retry after ack loss")
    assert again["status"] == "running"

    await repo.record_goal_run_event(gid, "goal_run.note", {"text": "halfway"})
    events = await repo.list_events(gid, limit=50)
    kinds = [event["event_type"] for event in events]
    assert "goal_run.created" in kinds
    assert kinds.count("goal_run.status_changed") >= 2
    assert "goal_run.note" in kinds

    completed = await repo.transition_goal_run(gid, "completed", reason="verifier passed")
    assert completed["status"] == "completed"


async def test_illegal_transition_fails_closed(tmp_path: Path) -> None:
    repo = await _repo(tmp_path / "p3.sqlite3")
    row = await repo.create_goal_run(goal="No shortcuts.")
    with pytest.raises(DurableExecutionError, match="cannot move created -> completed"):
        await repo.transition_goal_run(row["id"], "completed", reason="skip admission")
    # The failed move left no trace: still created, no status_changed event.
    assert (await repo.get_goal_run(row["id"]))["status"] == "created"
    assert await repo.list_events(row["id"], limit=50) == [
        event
        for event in await repo.list_events(row["id"], limit=50)
        if event["event_type"] == "goal_run.created"
    ]


async def test_state_and_events_survive_restart(tmp_path: Path) -> None:
    path = tmp_path / "p3.sqlite3"
    repo = await _repo(path)
    row = await repo.create_goal_run(goal="Survive the crash.")
    gid = row["id"]
    await repo.transition_goal_run(gid, "admitted", reason="accepted")
    await repo.transition_goal_run(gid, "running", reason="launched")
    await repo.record_goal_run_event(gid, "goal_run.attempt", {"n": 1})

    # The crash analogue: a fresh repository over the same file.
    restarted = await _repo(path)
    restored = await restarted.get_goal_run(gid)
    assert restored is not None
    assert restored["status"] == "running"
    assert restored["goal_text"] == "Survive the crash."
    kinds = [event["event_type"] for event in await restarted.list_events(gid, limit=50)]
    assert "goal_run.created" in kinds
    assert "goal_run.status_changed" in kinds
    assert "goal_run.attempt" in kinds

    # Recovery continues from the restored state, not from scratch.
    recovering = await restarted.transition_goal_run(gid, "recovering", reason="restart detected")
    assert recovering["status"] == "recovering"
    recovered = await restarted.transition_goal_run(gid, "recovered", reason="verified")
    assert recovered["status"] == "recovered"


async def test_create_is_idempotent_on_dispatch_key(tmp_path: Path) -> None:
    repo = await _repo(tmp_path / "p3.sqlite3")
    first = await repo.create_goal_run(goal="Once.", idempotency_key="dispatch:abc")
    second = await repo.create_goal_run(goal="Twice.", idempotency_key="dispatch:abc")
    assert first["id"] == second["id"]
