"""docs/VEYA_P1_P3_IMPLEMENTATION_SPEC.md §16 Trajectory — standalone module tests."""

from __future__ import annotations

from server.trajectory import append_trajectory, build_trajectory, read_trajectories


def test_build_trajectory_success_has_no_failures():
    t = build_trajectory(
        task_id="t1",
        objective="do the thing",
        outcome="success",
        tool_calls=[{"tool": "grep", "ok": True}],
        duration_ms=42,
    )
    assert t.task_id == "t1"
    assert t.objective == "do the thing"
    assert t.tool_calls == [{"tool": "grep", "ok": True}]
    assert t.duration_ms == 42
    assert t.failures == []
    assert t.acceptance_results == []
    assert t.recovery_actions == []
    assert t.cost_usd == 0.0
    assert t.trace_id == ""


def test_build_trajectory_failure_records_error():
    t = build_trajectory(
        task_id="t2",
        objective="do the thing",
        outcome="failed",
        tool_calls=[],
        duration_ms=10,
        error="boom",
    )
    assert t.failures == [{"error": "boom"}]


def test_append_and_read_round_trip(tmp_path):
    path = tmp_path / "sess.jsonl"
    t1 = build_trajectory(
        task_id="sess", objective="first", outcome="completed", tool_calls=[], duration_ms=1
    )
    t2 = build_trajectory(
        task_id="sess", objective="second", outcome="failed", tool_calls=[], duration_ms=2
    )
    append_trajectory(t1, path=path)
    append_trajectory(t2, path=path)

    records = read_trajectories("sess", path=path)
    assert [r["objective"] for r in records] == ["first", "second"]
    assert [r["outcome"] for r in records] == ["completed", "failed"]


def test_trajectory_records_acceptance_and_recovery(tmp_path):
    t = build_trajectory(
        task_id="task-x",
        objective="resume safely",
        outcome="completed",
        tool_calls=[],
        duration_ms=10,
        acceptance_results=[{"id": "c1", "status": "passed"}],
        recovery_actions=[{"action": "loaded_checkpoint", "checkpoint_id": "cp1"}],
        cost_usd=0.12,
        trace_id="trace-x",
    )
    append_trajectory(t, path=tmp_path / "trajectory.jsonl")
    row = read_trajectories("task-x", path=tmp_path / "trajectory.jsonl")[0]
    assert row["acceptance_results"][0]["status"] == "passed"
    assert row["recovery_actions"][0]["checkpoint_id"] == "cp1"
    assert row["trace_id"] == "trace-x"


def test_read_trajectories_missing_file_returns_empty(tmp_path):
    assert read_trajectories("nope", path=tmp_path / "missing.jsonl") == []
