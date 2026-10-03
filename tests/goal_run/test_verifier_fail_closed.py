"""P4 fail-closed verifier: only a verification PASS allows COMPLETED.

Pins the gate at both layers:
- reconcile_task (sync Phase-B gate on the MCP dispatch path): missing
  artifact, bad artifact and verifier crash all land BLOCKED, never
  COMPLETED, and a verifier exception never propagates.
- verify_task (async path): an unexpected internal error returns
  passed=False instead of raising.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from server.goal_run.models import TaskStatus
from server.goal_run.pre_admission import (
    create_pending_run,
    load_goal_run,
    reconcile_task,
    save_goal_run,
)
from server.goal_run.verify import verify_task
from tests.goal_run.test_verify_mechanical import _task


def _admitted(tmp_path: Path, acceptance: list[str]) -> tuple[str, str]:
    pre = create_pending_run(
        project_root=str(tmp_path),
        dispatch_id="p4-dispatch",
        session_id="p4-session",
        requested_executor="codex",
        tasks=[{"worker": "codex", "task": "do it"}],
    )
    task_id = next(iter(pre.state.tasks))
    state = load_goal_run(str(tmp_path), pre.state.goal_id)
    assert state is not None
    state.tasks[task_id].acceptance = list(acceptance)
    state.tasks[task_id].status = TaskStatus.running
    save_goal_run(state, str(tmp_path))
    return pre.state.goal_id, task_id


def test_missing_artifact_blocks_completion(tmp_path: Path) -> None:
    goal_id, task_id = _admitted(tmp_path, ["文件 test_p4_probe.py exists"])
    assert not (tmp_path / "test_p4_probe.py").exists()
    state = reconcile_task(
        project_root=str(tmp_path),
        goal_run_id=goal_id,
        goal_task_id=task_id,
        outcome="completed",
        summary="1 passed, 0 failed",
    )
    assert state.tasks[task_id].status == TaskStatus.blocked
    from server.goal_run.models import GoalStatus

    assert state.status != GoalStatus.completed


def test_bad_artifact_blocks_completion(tmp_path: Path) -> None:
    goal_id, task_id = _admitted(tmp_path, ["all tests pass"])
    state = reconcile_task(
        project_root=str(tmp_path),
        goal_run_id=goal_id,
        goal_task_id=task_id,
        outcome="completed",
        summary="3 passed, 2 failed in 0.10s",
    )
    assert state.tasks[task_id].status == TaskStatus.blocked
    assert "2 failed" in (state.tasks[task_id].block_reason or "")


def test_verifier_crash_blocks_completion(tmp_path: Path, monkeypatch) -> None:
    def _boom(*_a, **_k):
        raise RuntimeError("rule engine exploded")

    monkeypatch.setattr("server.goal_run.verify._rule_check", _boom)
    goal_id, task_id = _admitted(tmp_path, ["all tests pass"])
    # Must not raise: a broken verifier is a failed verification.
    state = reconcile_task(
        project_root=str(tmp_path),
        goal_run_id=goal_id,
        goal_task_id=task_id,
        outcome="completed",
        summary="5 passed, 0 failed",
    )
    assert state.tasks[task_id].status == TaskStatus.blocked
    assert "verifier error" in (state.tasks[task_id].block_reason or "")


def test_valid_artifact_completes(tmp_path: Path) -> None:
    (tmp_path / "test_p4_probe.py").write_text("def test_x():\n    assert True\n")
    goal_id, task_id = _admitted(tmp_path, ["文件 test_p4_probe.py exists"])
    state = reconcile_task(
        project_root=str(tmp_path),
        goal_run_id=goal_id,
        goal_task_id=task_id,
        outcome="completed",
        summary="1 passed, 0 failed",
    )
    assert state.tasks[task_id].status == TaskStatus.completed


@pytest.mark.asyncio
async def test_verify_task_internal_error_fails_closed(tmp_path, monkeypatch) -> None:
    def _boom(*_a, **_k):
        raise RuntimeError("mechanical engine exploded")

    monkeypatch.setattr("server.goal_run.verify._mechanical_verify", _boom)
    result = await verify_task(_task(["all tests pass"]), "5 passed, 0 failed", str(tmp_path))
    assert result.passed is False
    assert result.reason


@pytest.mark.asyncio
async def test_verify_task_missing_artifact_fails(tmp_path) -> None:
    result = await verify_task(
        _task(["文件 test_p4_missing.py exists"]), "5 passed, 0 failed", str(tmp_path)
    )
    assert result.passed is False
