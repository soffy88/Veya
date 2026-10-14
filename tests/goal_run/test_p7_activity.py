"""P7: unified execution activity stream.

Proves:
- the canonical activity vocabulary exists
- one monotonic sequence per GoalRun, durable across restart
- cursor stream resumes exactly
- output is bounded and spills to an artifact
- observability snapshot answers "what is running now"
"""

from __future__ import annotations

from pathlib import Path

from server.goal_run.execution_activity import (
    OUTPUT_TAIL_BYTES,
    ActivityRecorder,
    ActivityType,
    ObservabilitySnapshot,
    observability_snapshot,
    stream_activities,
)
from server.goal_run.models import GoalRunState, GoalStatus, TaskNode
from server.goal_run.store import save_goal_run

_REQUIRED_TYPES = {
    "ADMISSION_CREATED",
    "ADMISSION_RELEASED",
    "EXECUTOR_SELECTED",
    "EXECUTOR_HEALTH_CHANGED",
    "BINDING_CREATED",
    "BINDING_UPDATED",
    "BINDING_LOST",
    "PROCESS_STARTED",
    "PROCESS_EXITED",
    "TOOL_STARTED",
    "TOOL_COMPLETED",
    "TOOL_FAILED",
    "OUTPUT",
    "TEST_STARTED",
    "TEST_COMPLETED",
    "BUILD_STARTED",
    "BUILD_COMPLETED",
    "STEER_REQUESTED",
    "STEER_COMPLETED",
    "CANCEL_REQUESTED",
    "CANCEL_COMPLETED",
    "PAUSE_REQUESTED",
    "PAUSE_COMPLETED",
    "RESUME_REQUESTED",
    "RESUME_COMPLETED",
    "RECOVERY_STARTED",
    "RECOVERY_COMPLETED",
    "RECOVERY_FAILED",
    "VERIFICATION_STARTED",
    "VERIFICATION_COMPLETED",
    "GOALRUN_COMPLETED",
    "GOALRUN_FAILED",
}


def _goal(
    tmp_path: Path, *, goal_id: str = "goal_p7", status: GoalStatus = GoalStatus.running
) -> str:
    state = GoalRunState(
        goal_id=goal_id, goal_text="p7", status=status, requested_executor="local2"
    )
    state.tasks["t1"] = TaskNode(
        id="t1", title="t", instruction="i", acceptance=["a"], depends_on=[], assignee="local2"
    )
    state.execution_id = "exec_p7"
    save_goal_run(state, str(tmp_path))
    return goal_id


def test_activity_type_vocabulary_complete():
    values = {t.value for t in ActivityType}
    assert values >= _REQUIRED_TYPES


def test_record_monotonic_sequence(tmp_path: Path):
    goal_id = _goal(tmp_path)
    rec = ActivityRecorder(str(tmp_path))
    a = rec.record(goal_id, ActivityType.PROCESS_STARTED, "RUNNING", {})
    b = rec.record(goal_id, ActivityType.OUTPUT, "RUNNING", {"text": "hi"})
    c = rec.record(goal_id, ActivityType.PROCESS_EXITED, "COMPLETED", {})
    assert [a.sequence, b.sequence, c.sequence] == [1, 2, 3]
    assert rec.latest_sequence(goal_id) == 3


def test_sequence_survives_restart(tmp_path: Path):
    goal_id = _goal(tmp_path)
    rec1 = ActivityRecorder(str(tmp_path))
    rec1.record(goal_id, ActivityType.PROCESS_STARTED, "RUNNING", {})
    rec1.record(goal_id, ActivityType.OUTPUT, "RUNNING", {"text": "x"})
    # New recorder instance = process restart; sequence must not reset.
    rec2 = ActivityRecorder(str(tmp_path))
    nxt = rec2.record(goal_id, ActivityType.GOALRUN_COMPLETED, "COMPLETED", {})
    assert nxt.sequence == 3


def test_stream_cursor_resume(tmp_path: Path):
    goal_id = _goal(tmp_path)
    rec = ActivityRecorder(str(tmp_path))
    for i in range(5):
        rec.record(goal_id, ActivityType.OUTPUT, "RUNNING", {"n": i})

    first, cursor = rec.stream(goal_id, cursor=0)
    assert [a.sequence for a in first] == [1, 2, 3, 4, 5]
    assert cursor == 5

    rec.record(goal_id, ActivityType.GOALRUN_COMPLETED, "COMPLETED", {})
    resumed, cursor2 = rec.stream(goal_id, cursor=cursor)
    assert [a.sequence for a in resumed] == [6]
    assert cursor2 == 6
    # No duplicates on replay.
    again, _ = rec.stream(goal_id, cursor=cursor2)
    assert again == []


def test_activity_fields_complete(tmp_path: Path):
    goal_id = _goal(tmp_path)
    rec = ActivityRecorder(str(tmp_path))
    a = rec.record(
        goal_id,
        ActivityType.BINDING_CREATED,
        "STARTING",
        {"k": "v"},
        binding_id="bind_1",
        provider_id="local2",
        provider_run_id="run_1",
    )
    assert a.activity_id
    assert a.goal_run_id == goal_id
    assert a.binding_id == "bind_1"
    assert a.provider_id == "local2"
    assert a.provider_run_id == "run_1"
    assert a.type == "BINDING_CREATED"
    assert a.phase == "STARTING"
    assert a.payload == {"k": "v"}
    assert a.timestamp > 0


def test_output_small_is_inline(tmp_path: Path):
    goal_id = _goal(tmp_path)
    rec = ActivityRecorder(str(tmp_path))
    a = rec.output(goal_id, "stdout", "small output")
    assert a.payload["text"] == "small output"
    assert "artifact" not in a.payload


def test_output_large_spills_to_artifact(tmp_path: Path):
    goal_id = _goal(tmp_path)
    rec = ActivityRecorder(str(tmp_path))
    big = "x" * (OUTPUT_TAIL_BYTES + 1000)
    a = rec.output(goal_id, "stdout", big)
    assert a.payload.get("truncated") is True
    assert a.payload.get("artifact")
    assert len(a.payload.get("tail", "")) <= OUTPUT_TAIL_BYTES
    # The artifact really exists and holds the full output.
    artifact = tmp_path / a.payload["artifact"]
    assert artifact.is_file()
    assert artifact.read_text() == big


def test_observability_snapshot_running(tmp_path: Path):
    goal_id = _goal(tmp_path, goal_id="goal_run_a", status=GoalStatus.running)
    rec = ActivityRecorder(str(tmp_path))
    rec.record(goal_id, ActivityType.PROCESS_STARTED, "RUNNING", {})
    snapshot = observability_snapshot(str(tmp_path))
    assert isinstance(snapshot, ObservabilitySnapshot)
    ids = [r["goal_run_id"] for r in snapshot.running]
    assert "goal_run_a" in ids


def test_observability_reports_recovery_required(tmp_path: Path):
    _goal(tmp_path, goal_id="goal_rec", status=GoalStatus.recovering)
    snapshot = observability_snapshot(str(tmp_path))
    ids = [r["goal_run_id"] for r in snapshot.recovery_required]
    assert "goal_rec" in ids


def test_module_level_stream_activities(tmp_path: Path):
    goal_id = _goal(tmp_path)
    rec = ActivityRecorder(str(tmp_path))
    rec.record(goal_id, ActivityType.TEST_STARTED, "TESTING", {})
    rec.record(goal_id, ActivityType.TEST_COMPLETED, "TESTING", {"passed": True})
    activities, cursor = stream_activities(str(tmp_path), goal_id, cursor=0)
    assert [a.type for a in activities] == ["TEST_STARTED", "TEST_COMPLETED"]
    assert cursor == 2
