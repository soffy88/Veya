"""W17 — supervisor bridge: remote evidence into W12 supervisor reports."""

from __future__ import annotations

import time
from dataclasses import dataclass

from server.goal_run.models import GoalRunState, GoalStatus, TaskNode, TaskStatus
from server.goal_run.store import save_goal_run
from server.remote_control.sessions import VerifierEvidence
from server.remote_control.supervisor_bridge import (
    bridge_bg_status_to_supervisor,
    inject_remote_evidence,
)
from server.supervisor.reporting import build_progress_report


@dataclass
class DummyBinding:
    goal_run_id: str
    objective: str = "Test objective"
    report_seq: int = 1
    last_activity_at: float = 0.0
    stall_after_minutes: int = 30
    nudges_sent: int = 0
    max_repairs: int = 3


def test_inject_remote_evidence_new_and_existing_task(tmp_path):
    root = str(tmp_path)
    gid = "goal_test_1"

    # 1. State does not exist
    ev = VerifierEvidence(
        verified=True, source="ssh_bg:lap:task1", summary="exit=0 tail=build success"
    )
    assert not inject_remote_evidence(project_root=root, goal_run_id=gid, evidence=ev)

    # 2. Create goal run state with initial task
    initial_state = GoalRunState(
        goal_id=gid,
        goal_text="Run build on remote host",
        status=GoalStatus.running,
        tasks={
            "t1": TaskNode(
                id="t1",
                title="Prepare",
                instruction="Init",
                acceptance=[],
                depends_on=[],
                assignee="system",
                status=TaskStatus.completed,
                execute_result="prepared",
            )
        },
    )
    save_goal_run(initial_state, root)

    # 3. Inject new remote task
    ok = inject_remote_evidence(
        project_root=root, goal_run_id=gid, evidence=ev, task_id="remote_build"
    )
    assert ok

    binding = DummyBinding(goal_run_id=gid, last_activity_at=time.time())
    report = build_progress_report(goal_run_id=gid, project_root=root, binding=binding)

    assert report["tasks_done"] == 2
    assert report["last_evidence"] is not None
    assert "exit=0 tail=build success" in report["last_evidence"]
    assert "最新证据" in report["body"] or "exit=0" in report["body"]


def test_bridge_bg_status_to_supervisor_reporting(tmp_path):
    root = str(tmp_path)
    gid = "goal_test_2"

    initial_state = GoalRunState(
        goal_id=gid,
        goal_text="Remote testing",
        status=GoalStatus.running,
        tasks={},
    )
    save_goal_run(initial_state, root)

    bg_status_ok = {
        "host": "lap",
        "name": "pytest_run",
        "finished": True,
        "exit_code": 0,
        "log_tail": "35 passed in 2.1s",
    }
    ev = bridge_bg_status_to_supervisor(
        project_root=root,
        goal_run_id=gid,
        bg_status=bg_status_ok,
        task_id="remote_pytest",
    )
    assert ev.verified
    assert ev.source == "ssh_bg:lap:pytest_run"

    binding = DummyBinding(goal_run_id=gid, last_activity_at=time.time())
    report = build_progress_report(goal_run_id=gid, project_root=root, binding=binding)

    assert report["tasks_done"] == 1
    assert "exit=0 tail=35 passed in 2.1s" in report["last_evidence"]
    assert report["is_done"]


def test_bridge_bg_status_failed_task(tmp_path):
    root = str(tmp_path)
    gid = "goal_test_3"

    initial_state = GoalRunState(
        goal_id=gid,
        goal_text="Remote testing fail",
        status=GoalStatus.running,
        tasks={},
    )
    save_goal_run(initial_state, root)

    bg_status_fail = {
        "host": "lap",
        "name": "failing_task",
        "finished": True,
        "exit_code": 1,
        "log_tail": "Error: test failure",
    }
    ev = bridge_bg_status_to_supervisor(
        project_root=root,
        goal_run_id=gid,
        bg_status=bg_status_fail,
        task_id="remote_fail",
    )
    assert not ev.verified

    binding = DummyBinding(goal_run_id=gid, last_activity_at=time.time())
    report = build_progress_report(goal_run_id=gid, project_root=root, binding=binding)
    assert report["tasks_done"] == 0
    assert not report["is_done"]
