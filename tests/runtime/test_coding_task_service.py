"""Tests for CodingTaskService — durable coding task lifecycle management.

Tests:
TASK-01 create
TASK-02 persisted
TASK-03 contract before worktree execution
TASK-04 GoalRun linked
TASK-05 resume
TASK-06 cancellation
TASK-07 partial completion
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest


@pytest.fixture
def test_repo(tmp_path: Path) -> Path:
    """Create a minimal test repository."""
    repo = tmp_path / "test_repo"
    repo.mkdir()

    # Initialize git repo
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"], cwd=repo, check=True, capture_output=True
    )

    # Create a simple file
    (repo / "main.py").write_text("def hello():\n    return 'hello'\n", encoding="utf-8")

    # Initial commit
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "Initial commit"], cwd=repo, check=True, capture_output=True
    )

    return repo


@pytest.mark.asyncio
async def test_task_01_create(test_repo: Path) -> None:
    """TASK-01: Create a coding task."""
    from runtime.coding.task_service import CodingTaskRequest, CodingTaskService

    service = CodingTaskService(str(test_repo))

    request = CodingTaskRequest(
        workspace_path=str(test_repo),
        objective="Add a goodbye function",
        source="cli",
    )

    state = await service.create_task(request)

    assert state.task_id is not None
    assert state.task_id.startswith("ct_")
    assert state.workspace_path == str(test_repo)
    assert state.objective == "Add a goodbye function"
    assert state.source == "cli"
    assert state.status in (
        "created",
        "contract_ready",
        "worktree_ready",
        "goalrun_created",
        "running",
    )


@pytest.mark.asyncio
async def test_task_02_persisted(test_repo: Path) -> None:
    """TASK-02: Task state is persisted to disk."""
    from runtime.coding.task_service import CodingTaskRequest, CodingTaskService, _read_task_state

    service = CodingTaskService(str(test_repo))

    request = CodingTaskRequest(
        workspace_path=str(test_repo),
        objective="Test persistence",
        source="cli",
    )

    state = await service.create_task(request)
    task_id = state.task_id

    # Read state from disk
    recovered = _read_task_state(test_repo, task_id)

    assert recovered is not None
    assert recovered.task_id == task_id
    assert recovered.objective == "Test persistence"
    assert recovered.workspace_path == str(test_repo)


@pytest.mark.asyncio
async def test_task_03_contract_before_worktree(test_repo: Path) -> None:
    """TASK-03: Contract is created before worktree execution."""
    from runtime.coding.task_service import CodingTaskRequest, CodingTaskService

    service = CodingTaskService(str(test_repo))

    request = CodingTaskRequest(
        workspace_path=str(test_repo),
        objective="Test contract order",
        source="cli",
    )

    state = await service.create_task(request)
    task_id = state.task_id

    # Verify contract exists
    contract_path = test_repo / ".veya" / "runs" / task_id / "inputs" / "harness_contract.json"
    assert contract_path.exists(), "Contract must exist before worktree execution"

    # Verify contract content
    contract_data = json.loads(contract_path.read_text(encoding="utf-8"))
    assert "workspace_id" in contract_data
    assert "required_sensors" in contract_data
    assert "permission_profile" in contract_data


@pytest.mark.asyncio
async def test_task_04_goalrun_linked(test_repo: Path) -> None:
    """TASK-04: GoalRun can be linked to CodingTask."""
    from runtime.coding.task_service import CodingTaskRequest, CodingTaskService

    service = CodingTaskService(str(test_repo))

    request = CodingTaskRequest(
        workspace_path=str(test_repo),
        objective="Test GoalRun link",
        source="cli",
    )

    state = await service.create_task(request)
    task_id = state.task_id

    # Simulate GoalRun creation
    goal_run_id = f"gr_{task_id}"
    state.goal_run_id = goal_run_id
    from runtime.coding.task_service import _write_task_state

    _write_task_state(test_repo, state)

    # Verify link persisted
    recovered = service.get_task_state(task_id)
    assert recovered is not None
    assert recovered.goal_run_id == goal_run_id


@pytest.mark.asyncio
async def test_task_05_resume(test_repo: Path) -> None:
    """TASK-05: Task can be resumed from durable state."""
    from runtime.coding.task_service import CodingTaskRequest, CodingTaskService, CodingTaskStatus

    service = CodingTaskService(str(test_repo))

    # Create initial task
    request = CodingTaskRequest(
        workspace_path=str(test_repo),
        objective="Test resume",
        source="cli",
    )

    state = await service.create_task(request)
    task_id = state.task_id

    # Simulate interruption by setting status to partial
    state.status = CodingTaskStatus.PARTIAL_COMPLETED.value
    from runtime.coding.task_service import _write_task_state

    _write_task_state(test_repo, state)

    # Resume with new service instance
    new_service = CodingTaskService(str(test_repo))
    resume_request = CodingTaskRequest(
        workspace_path=str(test_repo),
        objective="Test resume",
        source="cli",
        resume_task_id=task_id,
    )

    resumed_state = await new_service.create_task(resume_request)

    # Should return existing state, not create new task
    assert resumed_state.task_id == task_id
    assert resumed_state.status == CodingTaskStatus.PARTIAL_COMPLETED.value


@pytest.mark.asyncio
async def test_task_06_cancellation(test_repo: Path) -> None:
    """TASK-06: Task can be cancelled."""
    from runtime.coding.task_service import CodingTaskRequest, CodingTaskService, CodingTaskStatus

    service = CodingTaskService(str(test_repo))

    request = CodingTaskRequest(
        workspace_path=str(test_repo),
        objective="Test cancellation",
        source="cli",
    )

    state = await service.create_task(request)
    task_id = state.task_id

    # Cancel the task
    cancelled_state = await service.cancel_task(task_id)

    assert cancelled_state.status == CodingTaskStatus.CANCELLED.value

    # Verify persisted
    recovered = service.get_task_state(task_id)
    assert recovered is not None
    assert recovered.status == CodingTaskStatus.CANCELLED.value


@pytest.mark.asyncio
async def test_task_07_partial_completion(test_repo: Path) -> None:
    """TASK-07: Task can be in partial completion state."""
    from runtime.coding.task_service import CodingTaskRequest, CodingTaskService, CodingTaskStatus

    service = CodingTaskService(str(test_repo))

    request = CodingTaskRequest(
        workspace_path=str(test_repo),
        objective="Test partial completion",
        source="cli",
    )

    state = await service.create_task(request)
    task_id = state.task_id

    # Simulate partial completion
    state.status = CodingTaskStatus.PARTIAL_COMPLETED.value
    state.final_result = {
        "acceptance_passed": False,
        "error": "Tests still failing",
    }
    from runtime.coding.task_service import _write_task_state

    _write_task_state(test_repo, state)

    # Verify state
    recovered = service.get_task_state(task_id)
    assert recovered is not None
    assert recovered.status == CodingTaskStatus.PARTIAL_COMPLETED.value
    assert recovered.final_result is not None
    assert recovered.final_result.get("acceptance_passed") is False


@pytest.mark.asyncio
async def test_worktree_isolation(test_repo: Path) -> None:
    """Test that worktree is isolated from main repo."""
    from runtime.coding.task_service import CodingTaskRequest, CodingTaskService

    service = CodingTaskService(str(test_repo))

    request = CodingTaskRequest(
        workspace_path=str(test_repo),
        objective="Test isolation",
        source="cli",
    )

    state = await service.create_task(request)

    # Verify worktree path
    assert state.worktree_path is not None
    worktree_path = Path(state.worktree_path)

    # Worktree should be under .veya/worktrees
    assert ".veya" in worktree_path.parts
    assert "worktrees" in worktree_path.parts

    # Worktree should have its own .git
    assert (worktree_path / ".git").exists()

    # Worktree should have a different branch
    result = subprocess.run(
        ["git", "branch", "--show-current"],
        cwd=worktree_path,
        capture_output=True,
        text=True,
    )
    branch = result.stdout.strip()
    # Branch name may include prefix like "veya/"
    assert "task-" in branch or "ct_" in branch or "test-isolation" in branch


@pytest.mark.asyncio
async def test_list_tasks(test_repo: Path) -> None:
    """Test listing all coding tasks."""
    from runtime.coding.task_service import CodingTaskRequest, CodingTaskService

    service = CodingTaskService(str(test_repo))

    # Create multiple tasks
    for i in range(3):
        request = CodingTaskRequest(
            workspace_path=str(test_repo),
            objective=f"Task {i}",
            source="cli",
        )
        await service.create_task(request)

    # List tasks
    tasks = service.list_tasks()

    assert len(tasks) >= 3
    # Should be sorted by created_at descending
    assert all(t.task_id for t in tasks)


@pytest.mark.asyncio
async def test_task_artifacts(test_repo: Path) -> None:
    """Test getting task artifacts."""
    from runtime.coding.task_service import CodingTaskRequest, CodingTaskService

    service = CodingTaskService(str(test_repo))

    request = CodingTaskRequest(
        workspace_path=str(test_repo),
        objective="Test artifacts",
        source="cli",
    )

    state = await service.create_task(request)
    task_id = state.task_id

    # Create a test artifact
    outputs_dir = test_repo / ".veya" / "runs" / task_id / "outputs"
    outputs_dir.mkdir(parents=True, exist_ok=True)
    (outputs_dir / "test_artifact.txt").write_text("test content", encoding="utf-8")

    # Get artifacts
    artifacts = service.get_task_artifacts(task_id)

    assert len(artifacts) >= 1
    assert any("test_artifact.txt" in a.get("name", "") for a in artifacts)


__all__ = [
    "test_list_tasks",
    "test_task_01_create",
    "test_task_02_persisted",
    "test_task_03_contract_before_worktree",
    "test_task_04_goalrun_linked",
    "test_task_05_resume",
    "test_task_06_cancellation",
    "test_task_07_partial_completion",
    "test_task_artifacts",
    "test_worktree_isolation",
]
