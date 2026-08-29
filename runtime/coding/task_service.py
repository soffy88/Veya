"""Coding Task Service — durable coding task lifecycle management.

This module owns the CodingTask entity lifecycle:
CREATED → CONTRACT_READY → WORKTREE_READY → GOALRUN_CREATED → RUNNING
→ VERIFYING → FINALIZING → COMPLETED
Exception states: WAITING_APPROVAL, PARTIAL_COMPLETED, FAILED, CANCELLED

Persistence: .veya/runs/<task_id>/task.json (immutable append-only log)
Recovery: On process crash, reads existing task.json, contract, worktree, goal_run_id
"""

from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any, Literal

from runtime.coding.models import CodingTask
from runtime.coding.workspace_detect import detect_workspace
from runtime.coding.worktree import WorktreeError, WorktreeManager
from runtime.harness.contract import (
    build_coding_harness_contract,
    write_coding_harness_contract,
)
from runtime.harness.guides import load_guides
from runtime.harness.sensors import sensors_for_workspace


class CodingTaskStatus(str, Enum):
    CREATED = "created"
    CONTRACT_READY = "contract_ready"
    WORKTREE_READY = "worktree_ready"
    GOALRUN_CREATED = "goalrun_created"
    RUNNING = "running"
    VERIFYING = "verifying"
    FINALIZING = "finalizing"
    COMPLETED = "completed"
    WAITING_APPROVAL = "waiting_approval"
    PARTIAL_COMPLETED = "partial_completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class CodingTaskSource(str, Enum):
    CHAT = "chat"
    CLI = "cli"
    API = "api"


@dataclass
class CodingTaskRequest:
    workspace_path: str
    objective: str
    source: Literal["chat", "cli", "api"]
    resume_task_id: str | None = None
    max_wall_seconds: int | None = None


@dataclass
class CodingTaskResult:
    task_id: str
    goal_run_id: str | None
    status: Literal[
        "completed",
        "partial_completed",
        "failed",
        "cancelled",
        "waiting_approval",
    ]
    verification_report_id: str | None
    artifact_ids: list[str]
    changed_files: list[str]
    final_summary: str
    acceptance_passed: bool

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "task_id": self.task_id,
            "goal_run_id": self.goal_run_id,
            "status": self.status,
            "verification_report_id": self.verification_report_id,
            "artifact_ids": self.artifact_ids,
            "changed_files": self.changed_files,
            "final_summary": self.final_summary,
            "acceptance_passed": self.acceptance_passed,
        }


@dataclass
class CodingTaskState:
    """Durable state persisted to .veya/runs/<task_id>/task.json"""

    task_id: str
    workspace_path: str
    workspace_id: str
    objective: str
    source: str
    status: str
    goal_run_id: str | None = None
    branch_name: str | None = None
    worktree_path: str | None = None
    contract_path: str | None = None
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    completed_at: str | None = None
    max_wall_seconds: int | None = None
    error: str | None = None
    final_result: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CodingTaskState:
        return cls(**data)


def _task_dir(project_root: Path, task_id: str) -> Path:
    """Returns .veya/runs/<task_id>/ directory"""
    return project_root / ".veya" / "runs" / task_id


def _task_state_path(project_root: Path, task_id: str) -> Path:
    """Returns .veya/runs/<task_id>/task.json"""
    return _task_dir(project_root, task_id) / "task.json"


def _inputs_dir(project_root: Path, task_id: str) -> Path:
    """Returns .veya/runs/<task_id>/inputs/"""
    return _task_dir(project_root, task_id) / "inputs"


def _outputs_dir(project_root: Path, task_id: str) -> Path:
    """Returns .veya/runs/<task_id>/outputs/"""
    return _task_dir(project_root, task_id) / "outputs"


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    """Atomically write JSON to path."""
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(encoded, encoding="utf-8")
    tmp.replace(path)


def _read_task_state(project_root: Path, task_id: str) -> CodingTaskState | None:
    path = _task_state_path(project_root, task_id)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return CodingTaskState.from_dict(data)
    except (json.JSONDecodeError, TypeError, ValueError, KeyError):
        return None


def _write_task_state(project_root: Path, state: CodingTaskState) -> None:
    state.updated_at = datetime.now(UTC).isoformat()
    _atomic_write_json(_task_state_path(project_root, state.task_id), state.to_dict())


class CodingTaskService:
    """Service for creating, managing, and resuming coding tasks."""

    def __init__(self, project_root: str | Path):
        self.project_root = Path(project_root).expanduser().resolve()
        self._running_tasks: dict[str, asyncio.Task] = {}

    def _assert_repo(self) -> None:
        if not (self.project_root / ".git").exists():
            raise WorktreeError(f"not a Git repository: {self.project_root}")

    async def create_task(self, request: CodingTaskRequest) -> CodingTaskState:
        """Create a new coding task with full initialization."""
        self._assert_repo()

        # Generate task ID
        task_id = request.resume_task_id or f"ct_{uuid.uuid4().hex[:12]}"

        # Check if resuming
        if request.resume_task_id:
            existing = _read_task_state(self.project_root, request.resume_task_id)
            if existing:
                # Resume: verify workspace matches
                if existing.workspace_path != str(request.workspace_path):
                    raise WorktreeError(
                        f"Resume task {request.resume_task_id} belongs to different workspace"
                    )
                return existing

        # Initialize workspace
        workspace = detect_workspace(request.workspace_path)

        # Create CodingTask entity
        coding_task = CodingTask(
            id=task_id,
            workspace_id=workspace.id,
            goal_run_id=None,
            source=request.source,
            objective=request.objective,
            status="planned",
            branch_name=None,
            worktree_path=None,
        )

        # Build contract
        guides = load_guides(workspace)
        sensors = sensors_for_workspace(workspace, guides)
        contract = build_coding_harness_contract(
            workspace,
            task_id,
            guides=guides,
            sensors=sensors,
        )

        # Persist initial state
        state = CodingTaskState(
            task_id=task_id,
            workspace_path=str(request.workspace_path),
            workspace_id=workspace.id,
            objective=request.objective,
            source=request.source,
            status=CodingTaskStatus.CREATED.value,
            max_wall_seconds=request.max_wall_seconds,
        )
        _write_task_state(self.project_root, state)

        # Phase 1: Contract ready
        state.status = CodingTaskStatus.CONTRACT_READY.value
        contract_file = write_coding_harness_contract(self.project_root, task_id, contract)
        state.contract_path = str(contract_file)
        _write_task_state(self.project_root, state)

        # Phase 2: Worktree ready
        state.status = CodingTaskStatus.WORKTREE_READY.value
        manager = WorktreeManager(workspace)
        record = manager.create(task_id, request.objective)
        state.worktree_path = record.path
        state.branch_name = record.branch_name
        coding_task.worktree_path = record.path
        coding_task.branch_name = record.branch_name
        coding_task.status = "running"
        _write_task_state(self.project_root, state)

        # Phase 3: GoalRun created (placeholder - actual GoalRun linking in PHASE 4)
        state.status = CodingTaskStatus.GOALRUN_CREATED.value
        _write_task_state(self.project_root, state)

        # Phase 4: Running
        state.status = CodingTaskStatus.RUNNING.value
        _write_task_state(self.project_root, state)

        return state

    async def run_task(
        self,
        task_id: str,
        leaf_sequence: list[dict[str, Any]],
        *,
        goal_run_id: str | None = None,
    ) -> CodingTaskResult:
        """Execute a coding task's leaf sequence.

        leaf_sequence: List of {tool, args} dicts to execute in order.
        Each leaf returns DelegateResult-compatible evidence.
        """
        state = _read_task_state(self.project_root, task_id)
        if not state:
            raise WorktreeError(f"Task not found: {task_id}")

        if goal_run_id:
            state.goal_run_id = goal_run_id
            _write_task_state(self.project_root, state)

        # Execute each leaf
        all_evidence: list[dict[str, Any]] = []
        all_artifacts: list[str] = []
        changed_files: list[str] = []

        for i, leaf in enumerate(leaf_sequence):
            tool_name = leaf.get("tool")
            args = leaf.get("args", {})
            # Tool execution via registry
            from server.tool_registry import master_tools

            if not master_tools.has(tool_name):
                raise WorktreeError(f"Unknown tool: {tool_name}")

            try:
                result = await master_tools.execute(tool_name, args)
                all_evidence.append(
                    {
                        "step": i,
                        "tool": tool_name,
                        "result": result,
                    }
                )
                if result.get("artifacts"):
                    all_artifacts.extend(
                        [str(a.get("path", "")) for a in result["artifacts"] if a.get("path")]
                    )
                if result.get("data", {}).get("worktree", {}).get("changed_files"):
                    changed_files.extend(result["data"]["worktree"]["changed_files"])
            except Exception as e:
                state.status = CodingTaskStatus.FAILED.value
                state.error = f"Leaf {i} ({tool_name}) failed: {type(e).__name__}: {e}"
                _write_task_state(self.project_root, state)
                return CodingTaskResult(
                    task_id=task_id,
                    goal_run_id=state.goal_run_id,
                    status="failed",
                    verification_report_id=None,
                    artifact_ids=[],
                    changed_files=[],
                    final_summary=f"Task failed at step {i}: {e}",
                    acceptance_passed=False,
                )

        # Phase: Verifying
        state.status = CodingTaskStatus.VERIFYING.value
        _write_task_state(self.project_root, state)

        # Phase: Finalizing
        state.status = CodingTaskStatus.FINALIZING.value
        _write_task_state(self.project_root, state)

        # Finalize via coding_finalize_patch
        from server.tool_registry import master_tools

        finalize_result = await master_tools.execute(
            "coding_finalize_patch",
            {"worktree_path": state.worktree_path},
        )

        acceptance_passed = finalize_result.get("data", {}).get("patch", {}).get("verified", False)
        verification_report_id = (
            finalize_result.get("data", {}).get("verification_report", {}).get("id")
        )

        # Phase: Completed / Partial
        if acceptance_passed:
            state.status = CodingTaskStatus.COMPLETED.value
            final_status = "completed"
        else:
            state.status = CodingTaskStatus.PARTIAL_COMPLETED.value
            final_status = "partial_completed"

        state.completed_at = datetime.now(UTC).isoformat()
        state.final_result = {
            "verification_report_id": verification_report_id,
            "artifact_ids": all_artifacts,
            "changed_files": changed_files,
            "acceptance_passed": acceptance_passed,
        }
        _write_task_state(self.project_root, state)

        return CodingTaskResult(
            task_id=task_id,
            goal_run_id=state.goal_run_id,
            status=final_status,
            verification_report_id=verification_report_id,
            artifact_ids=all_artifacts,
            changed_files=changed_files,
            final_summary=finalize_result.get("data", {}).get("summary", ""),
            acceptance_passed=acceptance_passed,
        )

    async def resume_task(
        self, task_id: str, new_leaves: list[dict[str, Any]] | None = None
    ) -> CodingTaskResult:
        """Resume a coding task from its durable state."""
        state = _read_task_state(self.project_root, task_id)
        if not state:
            raise WorktreeError(f"Task not found: {task_id}")

        if state.status in (CodingTaskStatus.COMPLETED.value, CodingTaskStatus.CANCELLED.value):
            # Return existing result
            result = state.final_result or {}
            return CodingTaskResult(
                task_id=task_id,
                goal_run_id=state.goal_run_id,
                status=state.status,
                verification_report_id=result.get("verification_report_id"),
                artifact_ids=result.get("artifact_ids", []),
                changed_files=result.get("changed_files", []),
                final_summary="Resumed completed task",
                acceptance_passed=result.get("acceptance_passed", False),
            )

        # Restore status to RUNNING
        state.status = CodingTaskStatus.RUNNING.value
        _write_task_state(self.project_root, state)

        # If new leaves provided, run them
        if new_leaves:
            return await self.run_task(task_id, new_leaves, goal_run_id=state.goal_run_id)

        # Otherwise just return current state
        result = state.final_result or {}
        return CodingTaskResult(
            task_id=task_id,
            goal_run_id=state.goal_run_id,
            status=state.status,
            verification_report_id=result.get("verification_report_id"),
            artifact_ids=result.get("artifact_ids", []),
            changed_files=result.get("changed_files", []),
            final_summary="Resumed",
            acceptance_passed=result.get("acceptance_passed", False),
        )

    async def cancel_task(self, task_id: str) -> CodingTaskState:
        """Cancel a running task."""
        state = _read_task_state(self.project_root, task_id)
        if not state:
            raise WorktreeError(f"Task not found: {task_id}")

        # Cancel any running execution
        if task_id in self._running_tasks:
            self._running_tasks[task_id].cancel()
            del self._running_tasks[task_id]

        # If GoalRun exists, cancel it
        if state.goal_run_id:
            from server.goal_run.runner import cancel_goal

            try:
                await cancel_goal(str(self.project_root), state.goal_run_id)
            except Exception:
                pass

        state.status = CodingTaskStatus.CANCELLED.value
        _write_task_state(self.project_root, state)
        return state

    def get_task_state(self, task_id: str) -> CodingTaskState | None:
        """Read task state (read-only)."""
        return _read_task_state(self.project_root, task_id)

    def get_task_diff(self, task_id: str) -> dict[str, Any] | None:
        """Get diff for a task's worktree."""
        state = _read_task_state(self.project_root, task_id)
        if not state or not state.worktree_path:
            return None
        manager = WorktreeManager(self.project_root)
        try:
            return manager.diff(path=state.worktree_path)
        except WorktreeError:
            return None

    def get_task_artifacts(self, task_id: str) -> list[dict[str, Any]]:
        """List artifacts for a task."""
        outputs = _outputs_dir(self.project_root, task_id)
        if not outputs.is_dir():
            return []
        artifacts = []
        for f in outputs.iterdir():
            if f.is_file():
                artifacts.append({"name": f.name, "path": str(f), "size": f.stat().st_size})
        return artifacts

    def list_tasks(self) -> list[CodingTaskState]:
        """List all coding tasks."""
        runs_dir = self.project_root / ".veya" / "runs"
        if not runs_dir.is_dir():
            return []
        tasks = []
        for d in runs_dir.iterdir():
            if (d.is_dir() and d.name.startswith("ct_")) or d.name.startswith("task-"):
                state = _read_task_state(self.project_root, d.name)
                if state:
                    tasks.append(state)
        return sorted(tasks, key=lambda t: t.created_at, reverse=True)


__all__ = [
    "CodingTaskRequest",
    "CodingTaskResult",
    "CodingTaskService",
    "CodingTaskSource",
    "CodingTaskState",
    "CodingTaskStatus",
    "_read_task_state",
    "_task_state_path",
    "_write_task_state",
]
