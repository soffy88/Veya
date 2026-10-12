"""W12 Part A — Supervisor (flag VEYA_SUPERVISOR=1).

All offline. No network, no real model calls.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from tests.conftest import ROOT  # noqa: F401 — path bootstrap


@pytest.fixture
def supervisor_on(monkeypatch, tmp_path: Path):
    """Enable the supervisor flag and isolate its storage."""
    monkeypatch.setenv("VEYA_SUPERVISOR", "1")
    monkeypatch.setenv(
        "VEYA_SUPERVISOR_BINDING_PATH", str(tmp_path / "supervisor_bindings.json")
    )
    monkeypatch.setenv(
        "VEYA_ROUTINE_REGISTRY_PATH", str(tmp_path / "routine_registry.json")
    )
    monkeypatch.setenv(
        "VEYA_BOT_REGISTRY_PATH", str(tmp_path / "bot_registry.json")
    )
    from server.supervisor import binding

    yield binding


@pytest.fixture
def supervisor_off(monkeypatch):
    """Disable the supervisor flag (the default)."""
    monkeypatch.setenv("VEYA_SUPERVISOR", "0")
    yield


@pytest.fixture
def bot_ctx(monkeypatch):
    """Bind a fake bot identity for the duration of one test."""
    from server.goal_run import bot_identity

    token = bot_identity.bind_current_bot("bot_test")
    yield
    bot_identity.reset_current_bot(token)


# ---------------------------------------------------------------------------
# A2 — supervise_task tool
# ---------------------------------------------------------------------------


def test_supervise_task_disabled_by_default(supervisor_off, bot_ctx, tmp_path):
    from server.supervision_tools import supervise_task

    with pytest.raises(RuntimeError, match="supervisor is disabled"):
        supervise_task(project_root=str(tmp_path), objective="修好 X")


def test_supervise_task_creates_goalrun_routine_binding(
    supervisor_on, bot_ctx, tmp_path
):
    from server.goal_run.store import load_goal_run
    from server.goal_run.routine_registry import RoutineRegistry
    from server.supervision_tools import supervise_task

    result = supervise_task(
        project_root=str(tmp_path),
        objective="把 X 修好",
        report_every_minutes=30,
        stall_after_minutes=20,
        max_repairs=3,
    )

    assert result["goal_run_id"].startswith("goal_")
    assert result["routine_id"].startswith("supervise_")
    assert result["report_every_minutes"] == 30
    assert result["stall_after_minutes"] == 20
    assert result["max_repairs"] == 3

    # GoalRun exists and is running
    state = load_goal_run(str(tmp_path), result["goal_run_id"])
    assert state is not None
    assert state.goal_text == "把 X 修好"
    assert state.bot_id == "bot_test"

    # Routine exists in the registry
    registry = RoutineRegistry(bot_id="bot_test")
    routine = registry.get(result["routine_id"])
    assert routine is not None
    assert routine.trigger_topic == "schedule.trigger"
    assert routine.goal_run_id == result["goal_run_id"]

    # Binding exists and is open
    binding = supervisor_on.get_binding(result["binding"]["binding_id"])
    assert binding is not None
    assert binding.status == "open"
    assert binding.owner_id == "bot_test"
    assert binding.project_root == str(tmp_path)


# ---------------------------------------------------------------------------
# A2 — plain phrasing parser
# ---------------------------------------------------------------------------


def test_parse_supervisor_phrase_basic():
    from server.bot_team.turns import parse_supervisor_phrase

    result = parse_supervisor_phrase("把 X 修好，每 30 分钟告诉我进度")
    assert result is not None
    assert result["report_every_minutes"] == 30
    assert result["stall_after_minutes"] == 15


def test_parse_supervisor_phrase_short_forms():
    from server.bot_team.turns import parse_supervisor_phrase

    for text in (
        "每5分钟汇报",
        "每隔 10 分钟告诉我进展",
        "每 60 分钟给我进度",
        "每 15 分钟上报一次状态",
    ):
        result = parse_supervisor_phrase(text)
        assert result is not None, f"failed to parse: {text}"
        assert result["report_every_minutes"] > 0


def test_parse_supervisor_phrase_no_cadence_returns_none():
    from server.bot_team.turns import parse_supervisor_phrase

    assert parse_supervisor_phrase("把 X 修好") is None
    assert parse_supervisor_phrase("") is None
    assert parse_supervisor_phrase("帮我看看") is None


def test_parse_supervisor_phrase_ignores_zero():
    from server.bot_team.turns import parse_supervisor_phrase

    assert parse_supervisor_phrase("每 0 分钟汇报") is None


# ---------------------------------------------------------------------------
# A3 — progress report from GoalRun state only
# ---------------------------------------------------------------------------


def _make_state(tmp_path: Path, goal_id: str, tasks: dict, status: str):
    """Build and persist a GoalRunState with the given task map."""
    from server.goal_run.models import GoalRunState, GoalStatus, TaskNode, TaskStatus

    state = GoalRunState(
        goal_id=goal_id,
        goal_text="把 X 修好",
        status=GoalStatus(status),
        bot_id="bot_test",
    )
    for tid, (title, status_value, execute_result) in tasks.items():
        state.tasks[tid] = TaskNode(
            id=tid,
            title=title,
            instruction=f"do {title}",
            acceptance=["works"],
            depends_on=[],
            assignee="worker",
            status=TaskStatus(status_value),
            execute_result=execute_result,
        )
    from server.goal_run.store import save_goal_run

    save_goal_run(state, str(tmp_path))
    return state


def test_report_shows_progress(supervisor_on, bot_ctx, tmp_path):
    from server.supervisor.binding import SupervisorBinding
    from server.supervisor.reporting import build_progress_report

    goal_id = "goal_test_001"
    _make_state(
        tmp_path,
        goal_id,
        {
            "t1": ("任务1", "completed", "evidence-1"),
            "t2": ("任务2", "running", None),
            "t3": ("任务3", "pending", None),
        },
        "running",
    )
    binding = SupervisorBinding(
        binding_id="b1",
        goal_run_id=goal_id,
        routine_id="r1",
        owner_id="bot_test",
        bot_id="bot_test",
        objective="把 X 修好",
        project_root=str(tmp_path),
        report_every_minutes=30,
        stall_after_minutes=20,
        max_repairs=3,
        last_activity_at=time.time(),
    )

    report = build_progress_report(
        goal_run_id=goal_id, project_root=str(tmp_path), binding=binding
    )
    assert report["tasks_done"] == 1
    assert report["tasks_total"] == 3
    assert report["tasks_blocked"] == 0
    assert report["status"] == "running"
    assert "已完成 1/3" in report["body"]
    assert report["last_evidence"] == "evidence-1"


def test_report_no_invented_progress(supervisor_on, bot_ctx, tmp_path):
    """A run with no completed tasks must not claim progress."""
    from server.supervisor.binding import SupervisorBinding
    from server.supervisor.reporting import build_progress_report

    goal_id = "goal_test_002"
    _make_state(
        tmp_path,
        goal_id,
        {"t1": ("任务1", "running", None)},
        "running",
    )
    binding = SupervisorBinding(
        binding_id="b2",
        goal_run_id=goal_id,
        routine_id="r2",
        owner_id="bot_test",
        bot_id="bot_test",
        objective="把 X 修好",
        project_root=str(tmp_path),
        stall_after_minutes=20,
        last_activity_at=time.time(),
    )
    report = build_progress_report(
        goal_run_id=goal_id, project_root=str(tmp_path), binding=binding
    )
    assert report["tasks_done"] == 0
    assert "尚未完成任何子任务" in report["body"]


def test_report_done_when_all_completed(supervisor_on, bot_ctx, tmp_path):
    from server.supervisor.binding import SupervisorBinding
    from server.supervisor.reporting import build_progress_report

    goal_id = "goal_test_003"
    _make_state(
        tmp_path,
        goal_id,
        {
            "t1": ("任务1", "completed", "done-1"),
            "t2": ("任务2", "completed", "done-2"),
        },
        "completed",
    )
    binding = SupervisorBinding(
        binding_id="b3",
        goal_run_id=goal_id,
        routine_id="r3",
        owner_id="bot_test",
        bot_id="bot_test",
        objective="把 X 修好",
        project_root=str(tmp_path),
        last_activity_at=time.time(),
    )
    report = build_progress_report(
        goal_run_id=goal_id, project_root=str(tmp_path), binding=binding
    )
    assert report["is_done"] is True
    assert "任务完成" in report["body"]
    assert report["last_evidence"] is not None


def test_report_stall_detected(supervisor_on, bot_ctx, tmp_path):
    """No GoalRun activity for stall_after_minutes → is_stalled."""
    from server.supervisor.binding import SupervisorBinding
    from server.supervisor.reporting import build_progress_report

    goal_id = "goal_test_004"
    _make_state(
        tmp_path,
        goal_id,
        {"t1": ("任务1", "running", None)},
        "running",
    )
    binding = SupervisorBinding(
        binding_id="b4",
        goal_run_id=goal_id,
        routine_id="r4",
        owner_id="bot_test",
        bot_id="bot_test",
        objective="把 X 修好",
        project_root=str(tmp_path),
        stall_after_minutes=20,
        last_activity_at=time.time() - (25 * 60),  # 25 min ago
    )
    report = build_progress_report(
        goal_run_id=goal_id, project_root=str(tmp_path), binding=binding
    )
    assert report["is_stalled"] is True
    assert "卡住" in report["body"]


def test_report_not_stalled_when_recent(supervisor_on, bot_ctx, tmp_path):
    from server.supervisor.binding import SupervisorBinding
    from server.supervisor.reporting import build_progress_report

    goal_id = "goal_test_005"
    _make_state(
        tmp_path,
        goal_id,
        {"t1": ("任务1", "running", None)},
        "running",
    )
    binding = SupervisorBinding(
        binding_id="b5",
        goal_run_id=goal_id,
        routine_id="r5",
        owner_id="bot_test",
        bot_id="bot_test",
        objective="把 X 修好",
        project_root=str(tmp_path),
        stall_after_minutes=20,
        last_activity_at=time.time(),  # just now
    )
    report = build_progress_report(
        goal_run_id=goal_id, project_root=str(tmp_path), binding=binding
    )
    assert report["is_stalled"] is False


def test_report_missing_goalrun(supervisor_on, bot_ctx, tmp_path):
    """A binding whose GoalRun vanished still reports — no crash."""
    from server.supervisor.binding import SupervisorBinding
    from server.supervisor.reporting import build_progress_report

    binding = SupervisorBinding(
        binding_id="b6",
        goal_run_id="goal_nonexistent",
        routine_id="r6",
        owner_id="bot_test",
        bot_id="bot_test",
        objective="把 X 修好",
        project_root=str(tmp_path),
    )
    report = build_progress_report(
        goal_run_id="goal_nonexistent",
        project_root=str(tmp_path),
        binding=binding,
    )
    assert "找不到该任务的执行状态" in report["body"]


# ---------------------------------------------------------------------------
# A6 — REST ownership (unit-level, no HTTP server)
# ---------------------------------------------------------------------------


def test_owned_binding_rejects_other_bot(supervisor_on, bot_ctx, tmp_path):
    """A binding owned by bot A is invisible to bot B."""
    from server.goal_run.bot_identity import BotRegistry, BotSpec
    from server.supervisor.binding import SupervisorBinding

    # Seed bot A with a real owner
    registry = BotRegistry()
    registry._items["bot_a"] = BotSpec(
        bot_id="bot_a", owner_id="user_a", name="A", description="A bot"
    )
    registry._save()

    binding = SupervisorBinding(
        binding_id="b7",
        goal_run_id="goal_x",
        routine_id="r7",
        owner_id="bot_a",
        bot_id="bot_a",
        objective="X",
        project_root=str(tmp_path),
    )
    supervisor_on.save_binding(binding)

    # The route helper must refuse a request from bot_b
    from server.routes.supervised import _owned_binding

    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        _owned_binding("b7", "bot_b", {"user_id": "user_b", "username": "b"})
    assert exc.value.status_code == 404


def test_owned_binding_allows_owner(supervisor_on, bot_ctx, tmp_path):
    from server.goal_run.bot_identity import BotRegistry, BotSpec
    from server.supervisor.binding import SupervisorBinding

    registry = BotRegistry()
    registry._items["bot_a"] = BotSpec(
        bot_id="bot_a", owner_id="user_a", name="A", description="A bot"
    )
    registry._save()

    binding = SupervisorBinding(
        binding_id="b8",
        goal_run_id="goal_x",
        routine_id="r8",
        owner_id="bot_a",
        bot_id="bot_a",
        objective="X",
        project_root=str(tmp_path),
    )
    supervisor_on.save_binding(binding)

    from server.routes.supervised import _owned_binding

    result = _owned_binding(
        "b8", "bot_a", {"user_id": "user_a", "username": "a"}
    )
    assert result.binding_id == "b8"


# ---------------------------------------------------------------------------
# A7 — restart resume, no duplicate report_seq
# ---------------------------------------------------------------------------


def test_bindings_for_goal_resume(supervisor_on, bot_ctx, tmp_path):
    """Open bindings for a GoalRun survive restart and are resumable."""
    from server.supervisor.binding import SupervisorBinding

    for i in range(3):
        binding = SupervisorBinding(
            binding_id=f"b9_{i}",
            goal_run_id="goal_restart",
            routine_id=f"r9_{i}",
            owner_id="bot_test",
            bot_id="bot_test",
            objective="X",
            project_root=str(tmp_path),
            report_seq=i,
            status="open" if i < 2 else "closed",
        )
        supervisor_on.save_binding(binding)

    open_bindings = supervisor_on.bindings_for_goal("goal_restart")
    assert len(open_bindings) == 2  # closed one excluded
    seqs = sorted(b.report_seq for b in open_bindings)
    assert seqs == [0, 1]  # monotonic, no replay


# ---------------------------------------------------------------------------
# Binding CRUD
# ---------------------------------------------------------------------------


def test_binding_crud_roundtrip(supervisor_on, bot_ctx, tmp_path):
    from server.supervisor.binding import SupervisorBinding

    binding = SupervisorBinding(
        binding_id="b10",
        goal_run_id="goal_crud",
        routine_id="r10",
        owner_id="bot_test",
        bot_id="bot_test",
        objective="X",
        project_root=str(tmp_path),
    )
    supervisor_on.save_binding(binding)

    fetched = supervisor_on.get_binding("b10")
    assert fetched is not None
    assert fetched.goal_run_id == "goal_crud"

    # list by owner
    items = supervisor_on.list_bindings(owner_id="bot_test")
    assert any(b.binding_id == "b10" for b in items)

    # delete
    assert supervisor_on.delete_binding("b10") is True
    assert supervisor_on.get_binding("b10") is None
    assert supervisor_on.delete_binding("b10") is False
