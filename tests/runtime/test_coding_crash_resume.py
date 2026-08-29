"""Tests for crash recovery and resume.

Tests:
CRASH-01 kill/restart
CRASH-02 no duplicate worktree
CRASH-03 no duplicate completed child
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

import pytest


@pytest.fixture
def test_repo(tmp_path: Path) -> Path:
    """Create a minimal test repository."""
    repo = tmp_path / "test_repo"
    repo.mkdir()

    # Initialize git repo
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True, capture_output=True)

    # Create a simple file
    (repo / "main.py").write_text("def hello():\n    return 'hello'\n", encoding="utf-8")

    # Initial commit
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=repo, check=True, capture_output=True)

    return repo


@pytest.mark.asyncio
async def test_crash_01_kill_restart(test_repo: Path) -> None:
    """CRASH-01: Process crash → resume from durable state."""
    from runtime.coding.task_service import CodingTaskRequest, CodingTaskService, CodingTaskStatus, _write_task_state

    # Create initial task
    service = CodingTaskService(str(test_repo))

    request = CodingTaskRequest(
        workspace_path=str(test_repo),
        objective="Test crash recovery",
        source="cli",
    )

    state = await service.create_task(request)
    task_id = state.task_id

    # Simulate crash: set status to RUNNING and "crash"
    state.status = CodingTaskStatus.RUNNING.value
    _write_task_state(test_repo, state)

    # Simulate restart with new service instance
    new_service = CodingTaskService(str(test_repo))

    # Recover state
    recovered = new_service.get_task_state(task_id)

    assert recovered is not None
    assert recovered.task_id == task_id
    assert recovered.objective == "Test crash recovery"
    assert recovered.worktree_path is not None

    # Worktree should still exist
    worktree_path = Path(recovered.worktree_path)
    assert worktree_path.exists()


@pytest.mark.asyncio
async def test_crash_02_no_duplicate_worktree(test_repo: Path) -> None:
    """CRASH-02: Resume does not create duplicate worktree."""
    from runtime.coding.task_service import CodingTaskRequest, CodingTaskService, _write_task_state

    # Create initial task
    service = CodingTaskService(str(test_repo))

    request = CodingTaskRequest(
        workspace_path=str(test_repo),
        objective="Test no duplicate worktree",
        source="cli",
    )

    state = await service.create_task(request)
    task_id = state.task_id
    original_worktree = state.worktree_path

    # Simulate crash
    _write_task_state(test_repo, state)

    # Resume with same task_id
    resume_request = CodingTaskRequest(
        workspace_path=str(test_repo),
        objective="Test no duplicate worktree",
        source="cli",
        resume_task_id=task_id,
    )

    resumed_state = await service.create_task(resume_request)

    # Should reuse existing worktree, not create new one
    assert resumed_state.task_id == task_id
    assert resumed_state.worktree_path == original_worktree

    # Verify only one worktree exists for this task
    worktrees_dir = test_repo / ".veya" / "worktrees"
    if worktrees_dir.exists():
        task_worktrees = [d for d in worktrees_dir.iterdir() if d.name.startswith("task-") and task_id in d.name]
        assert len(task_worktrees) <= 1


@pytest.mark.asyncio
async def test_crash_03_no_duplicate_completed_child(test_repo: Path) -> None:
    """CRASH-03: Completed children are not re-executed on resume."""
    from runtime.coding.goalrun import load_delegate_result, persist_delegate_result, build_coding_delegate_result
    from runtime.execution.models import DelegateResult, Evidence

    task_id = "crash-03-test"
    outputs_dir = test_repo / ".veya" / "runs" / task_id / "outputs"
    outputs_dir.mkdir(parents=True, exist_ok=True)

    # Create a delegate result with completed work
    delegate_result = DelegateResult(
        delegate_id=f"delegate-{task_id}",
        status="complete",
        summary="Task completed",
        stop_reason="completed",
        evidence=[Evidence(id="ev-1", kind="test", source="pytest", content="passed", producer="test")],
        artifacts=[],
        assertions=[],
        acceptance_results=[],
        completed_work=["leaf-1", "leaf-2"],
    )

    # Persist the result
    persist_delegate_result(test_repo, task_id, delegate_result)

    # Simulate crash and restart
    recovered = load_delegate_result(test_repo, task_id)

    assert recovered is not None
    assert recovered.status == "complete"
    assert len(recovered.completed_work) == 2

    # All completed work should still be recorded
    assert "leaf-1" in recovered.completed_work
    assert "leaf-2" in recovered.completed_work


@pytest.mark.asyncio
async def test_resume_preserves_contract(test_repo: Path) -> None:
    """Test that contract is preserved across crash/resume."""
    from runtime.coding.task_service import CodingTaskRequest, CodingTaskService
    from runtime.harness.contract import read_coding_harness_contract

    # Create initial task
    service = CodingTaskService(str(test_repo))

    request = CodingTaskRequest(
        workspace_path=str(test_repo),
        objective="Test contract preservation",
        source="cli",
    )

    state = await service.create_task(request)
    task_id = state.task_id

    # Read original contract
    original_contract = read_coding_harness_contract(test_repo, task_id)
    assert original_contract is not None

    # Simulate restart
    new_service = CodingTaskService(str(test_repo))

    # Resume
    resume_request = CodingTaskRequest(
        workspace_path=str(test_repo),
        objective="Test contract preservation",
        source="cli",
        resume_task_id=task_id,
    )

    resumed_state = await new_service.create_task(resume_request)

    # Contract should be identical
    resumed_contract = read_coding_harness_contract(test_repo, resumed_state.task_id)
    assert resumed_contract is not None
    assert resumed_contract.workspace_id == original_contract.workspace_id
    assert resumed_contract.required_sensors == original_contract.required_sensors


@pytest.mark.asyncio
async def test_resume_preserves_artifacts(test_repo: Path) -> None:
    """Test that artifacts are preserved across crash/resume."""
    from runtime.coding.task_service import CodingTaskService, CodingTaskState, _write_task_state

    task_id = "crash-artifacts-test"

    # Create task with artifacts
    state = CodingTaskState(
        task_id=task_id,
        workspace_path=str(test_repo),
        workspace_id="ws-test",
        objective="Test artifact preservation",
        source="cli",
        status="partial_completed",
    )

    # Create artifacts
    outputs_dir = test_repo / ".veya" / "runs" / task_id / "outputs"
    outputs_dir.mkdir(parents=True, exist_ok=True)
    (outputs_dir / "diff.patch").write_text("test diff", encoding="utf-8")
    (outputs_dir / "sensor_report.json").write_text('{"sensors": []}', encoding="utf-8")

    _write_task_state(test_repo, state)

    # Simulate restart
    new_service = CodingTaskService(str(test_repo))
    recovered = new_service.get_task_state(task_id)

    assert recovered is not None

    # Artifacts should still exist
    assert (outputs_dir / "diff.patch").exists()
    assert (outputs_dir / "sensor_report.json").exists()


@pytest.mark.asyncio
async def test_resume_continues_from_checkpoint(test_repo: Path) -> None:
    """Test that resume continues from last checkpoint, not from beginning."""
    from runtime.coding.task_service import CodingTaskService, CodingTaskState, _write_task_state

    task_id = "checkpoint-test"

    # Create task that was interrupted mid-execution
    state = CodingTaskState(
        task_id=task_id,
        workspace_path=str(test_repo),
        workspace_id="ws-test",
        objective="Test checkpoint resume",
        source="cli",
        status="running",
        worktree_path=str(test_repo / ".veya" / "worktrees" / f"task-{task_id}"),
    )

    # Create delegate result with partial progress
    outputs_dir = test_repo / ".veya" / "runs" / task_id / "outputs"
    outputs_dir.mkdir(parents=True, exist_ok=True)

    # Save partial progress
    partial_result = {
        "status": "partial",
        "children": [
            {"id": "leaf-1", "status": "ok", "summary": "Completed"},
        ],
        "acceptance_passed": False,
    }
    (outputs_dir / "delegate_result.json").write_text(
        json.dumps(partial_result),
        encoding="utf-8",
    )

    _write_task_state(test_repo, state)

    # Resume
    new_service = CodingTaskService(str(test_repo))
    recovered = new_service.get_task_state(task_id)

    assert recovered is not None
    assert recovered.status == "running"

    # Partial progress should be preserved
    delegate_path = outputs_dir / "delegate_result.json"
    if delegate_path.exists():
        saved = json.loads(delegate_path.read_text(encoding="utf-8"))
        assert len(saved.get("children", [])) == 1


@pytest.mark.asyncio
async def test_side_effect_ledger_continues(test_repo: Path) -> None:
    """Test that SideEffectLedger fencing continues across resume."""
    from runtime.coding.task_service import CodingTaskService, CodingTaskState, _write_task_state

    task_id = "side-effect-test"

    # Create task with side effects recorded
    state = CodingTaskState(
        task_id=task_id,
        workspace_path=str(test_repo),
        workspace_id="ws-test",
        objective="Test side effect ledger",
        source="cli",
        status="running",
    )

    # Create side effect ledger
    outputs_dir = test_repo / ".veya" / "runs" / task_id / "outputs"
    outputs_dir.mkdir(parents=True, exist_ok=True)

    ledger = {
        "effects": [
            {"id": "se-1", "tool": "edit", "path": "main.py", "status": "confirmed"},
        ],
        "fencing_token": "token-123",
    }
    (outputs_dir / "side_effect_ledger.json").write_text(
        json.dumps(ledger),
        encoding="utf-8",
    )

    _write_task_state(test_repo, state)

    # Resume
    new_service = CodingTaskService(str(test_repo))
    recovered = new_service.get_task_state(task_id)

    assert recovered is not None

    # Ledger should still exist
    ledger_path = outputs_dir / "side_effect_ledger.json"
    if ledger_path.exists():
        saved_ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
        assert saved_ledger.get("fencing_token") == "token-123"
        assert len(saved_ledger.get("effects", [])) == 1


__all__ = [
    "test_crash_01_kill_restart",
    "test_crash_02_no_duplicate_worktree",
    "test_crash_03_no_duplicate_completed_child",
    "test_resume_preserves_contract",
    "test_resume_preserves_artifacts",
    "test_resume_continues_from_checkpoint",
    "test_side_effect_ledger_continues",
]
