"""ExecutionControl — Local2 execution lifecycle control plane (P3).

GoalRun remains the sole durable execution authority.
All state changes write to GoalRun, ExecutorBinding, and event stream.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from server.goal_run.execution_activity import ActivityRecorder
from server.goal_run.execution_admission import (
    BindingStatus,
    get_admission,
)
from server.goal_run.models import GoalRunState, GoalStatus, TaskStatus
from server.goal_run.pre_admission import (
    load_goal_run,
    mark_task_running,
    save_goal_run,
)

logger = logging.getLogger("veya.goal_run.execution_control")


class ExecutionControlPhase(StrEnum):
    """Lifecycle phases for ExecutionControl (tracks alongside ExecutionPhase)."""

    CREATED = "CREATED"
    ADMITTED = "ADMITTED"
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    WAITING = "WAITING"
    STEERING = "STEERING"
    PAUSING = "PAUSING"
    CANCELLING = "CANCELLING"
    TERMINATING = "TERMINATING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"
    LOST = "LOST"


@dataclass
class ExecutionControlResult:
    goal_run_id: str
    phase: ExecutionControlPhase
    goal_status: GoalStatus
    execution_id: str | None = None
    binding_id: str | None = None
    provider_session_id: str | None = None
    provider_run_id: str | None = None
    executor_id: str | None = None
    reason: str = ""
    timestamp: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    def to_dict(self) -> dict[str, Any]:
        data = {
            "goal_run_id": self.goal_run_id,
            "phase": self.phase.value,
            "goal_status": self.goal_status.value,
            "execution_id": self.execution_id,
            "binding_id": self.binding_id,
            "provider_session_id": self.provider_session_id,
            "provider_run_id": self.provider_run_id,
            "executor_id": self.executor_id,
            "reason": self.reason,
            "timestamp": self.timestamp,
        }
        return data


@dataclass
class StreamCursor:
    """Cursor for streaming execution activity."""

    sequence: int
    goal_run_id: str
    timestamp: float


@dataclass
class ActivityEvent:
    """Single execution activity event."""

    sequence: int
    goal_run_id: str
    event_type: str
    phase: str
    payload: dict[str, Any]
    provider_id: str | None = None
    provider_run_id: str | None = None
    timestamp: float = field(default_factory=time.time)


class ExecutionControl:
    """Execution lifecycle control plane.

    Coordinates GoalRun, ExecutionAdmission, ExecutorBinding, and provider adapters.
    Does not create new durable authorities.
    """

    def __init__(self, project_root: str) -> None:
        self.project_root = project_root
        self.admission = get_admission(project_root)
        self._running_waiters: dict[str, asyncio.Event] = {}
        # P7: one durable, monotonic activity recorder (single evidence stream).
        self._recorder = ActivityRecorder(project_root)

    # ── Activity Stream ──────────────────────────────────────────────

    async def _emit_activity(
        self,
        goal_run_id: str,
        event_type: str,
        phase: str,
        payload: dict[str, Any],
        provider_id: str | None = None,
        provider_run_id: str | None = None,
    ) -> int:
        """Record one execution activity (P7 durable monotonic sequence)."""
        activity = self._recorder.record(
            goal_run_id,
            event_type,
            phase,
            payload,
            provider_id=provider_id,
            provider_run_id=provider_run_id,
        )
        return activity.sequence

    async def _get_binding(self, goal_run_id: str) -> tuple[str | None, str | None, str | None]:
        """Get binding info for a GoalRun."""
        state = load_goal_run(self.project_root, goal_run_id)
        if not state:
            return None, None, None

        checkpoint = state.runtime_checkpoint or {}
        binding_id = checkpoint.get("binding_id")
        if not binding_id:
            return state.execution_id, None, None

        # Load binding record
        import json

        from server.goal_run.execution_admission import _admission_dir

        binding_path = _admission_dir(self.project_root) / f"binding_{binding_id}.json"
        if not binding_path.exists():
            return state.execution_id, binding_id, None

        try:
            data = json.loads(binding_path.read_text(encoding="utf-8"))
            return (
                data.get("provider_session_id"),
                data.get("provider_run_id"),
                data.get("executor_id"),
            )
        except Exception:
            return state.execution_id, binding_id, None

    # ── Start ────────────────────────────────────────────────────────

    async def start(self, goal_run_id: str) -> ExecutionControlResult:
        """Start execution for an admitted GoalRun.

        Flow: GoalRun -> ExecutionAdmission -> ExecutorBinding -> provider adapter.start()
        """
        state = load_goal_run(self.project_root, goal_run_id)
        if not state:
            raise ValueError(f"GoalRun {goal_run_id} not found")

        if state.status not in (
            GoalStatus.pending_execution,
            GoalStatus.running,
            GoalStatus.recovering,
        ):
            raise RuntimeError(f"GoalRun {goal_run_id} status {state.status.value} cannot start")

        # Get execution_id from binding checkpoint
        checkpoint = state.runtime_checkpoint or {}
        execution_id = checkpoint.get("execution_id")
        if not execution_id:
            execution_id = f"exec_{uuid.uuid4().hex[:12]}"
            state.execution_id = execution_id

        # Mark task as running
        if state.tasks:
            first_task_id = next(iter(state.tasks))
            mark_task_running(
                project_root=self.project_root, goal_run_id=goal_run_id, goal_task_id=first_task_id
            )

        # Emit activity
        await self._emit_activity(
            goal_run_id,
            "ADMISSION_CREATED",
            "STARTING",
            {"execution_id": execution_id},
        )

        # Update GoalRun status
        state.status = GoalStatus.running
        state.started_at = datetime.now(UTC)
        save_goal_run(state, self.project_root)

        await self._emit_activity(
            goal_run_id,
            "PROCESS_STARTED",
            "RUNNING",
            {"execution_id": execution_id, "executor": state.requested_executor},
            provider_id=state.requested_executor,
        )

        return ExecutionControlResult(
            goal_run_id=goal_run_id,
            phase=ExecutionControlPhase.RUNNING,
            goal_status=state.status,
            execution_id=execution_id,
            reason="Execution started",
        )

    # ── Adapter-driven execution ────────────────────────────────────

    async def run_task(
        self,
        goal_run_id: str,
        *,
        command: str,
        executor_id: str | None = None,
        timeout_s: float = 600.0,
    ) -> ExecutionControlResult:
        """Run one real execution through the bound ExecutorAdapter.

        Flow: GoalRun -> ExecutorAdapter.start() -> GoalRun terminal status.

        The adapter owns the process; this method owns the durable GoalRun
        transition. No second durable authority is created.
        """
        from veya.remote.executor_adapter import (
            AdapterStartRequest,
            ProgressReporter,
            get_executor_adapter,
        )

        state = load_goal_run(self.project_root, goal_run_id)
        if not state:
            raise ValueError(f"GoalRun {goal_run_id} not found")

        # C2.1 — TWO ROLES.
        # ``requested_executor`` is the ADMITTED executor (ExecutorRegistry
        # authority). The adapter key below is the PHYSICAL substrate. For a
        # native substrate such as ``local2`` these differ, and evidence must
        # name both instead of mislabelling the substrate as the executor.
        admitted_executor = state.requested_executor or "local2"
        substrate = executor_id or admitted_executor
        adapter = get_executor_adapter(substrate, self.project_root)
        if adapter is None:
            raise RuntimeError(f"no ExecutorAdapter registered for {substrate!r}")

        substrate_kind = ""
        executor_kind = ""
        try:
            substrate_kind = str(adapter.identity.executor_kind)
        except Exception:  # pragma: no cover - identity is best-effort evidence
            substrate_kind = ""
        try:
            from veya.remote.executor_registry import get_executor_registry

            executor_kind = str(get_executor_registry().identity(admitted_executor).executor_kind)
        except Exception:  # pragma: no cover - unregistered executor ids
            executor_kind = ""

        checkpoint = state.runtime_checkpoint or {}
        execution_id = str(checkpoint.get("execution_id") or f"exec_{uuid.uuid4().hex[:12]}")
        state.execution_id = execution_id

        task_id = next(iter(state.tasks), "")
        if task_id:
            mark_task_running(
                project_root=self.project_root, goal_run_id=goal_run_id, goal_task_id=task_id
            )

        await self._emit_activity(
            goal_run_id,
            "EXECUTOR_SELECTED",
            "STARTING",
            {
                # Both roles, explicitly named.
                "admitted_executor": admitted_executor,
                "executor_id": admitted_executor,
                "executor_kind": executor_kind,
                "substrate_id": substrate,
                "substrate_kind": substrate_kind,
            },
        )
        await self._emit_activity(
            goal_run_id, "PROCESS_STARTED", "RUNNING", {"execution_id": execution_id}
        )

        reporter = ProgressReporter(execution_id, goal_run_id, substrate, recorder=self._recorder)
        request = AdapterStartRequest(
            goal_run_id=goal_run_id,
            goal_task_id=task_id,
            execution_id=execution_id,
            task_text=command,
            worktree_path=self.project_root,
            repo_root=self.project_root,
            timeout_s=timeout_s,
        )
        result = await adapter.start(request, reporter)

        state = load_goal_run(self.project_root, goal_run_id)
        if state is None:
            raise RuntimeError(f"GoalRun {goal_run_id} disappeared during execution")
        if result.exit_code == 0 and result.failure_class is None:
            state.status = GoalStatus.completed
            if task_id and task_id in state.tasks:
                state.tasks[task_id].status = TaskStatus.completed
                state.completed_ids.add(task_id)
            activity = ("GOALRUN_COMPLETED", "COMPLETED")
        else:
            state.status = GoalStatus.failed
            state.last_stop_reason = result.detail or result.failure_class or "execution failed"
            if task_id and task_id in state.tasks:
                state.tasks[task_id].status = TaskStatus.blocked
                state.tasks[task_id].block_reason = state.last_stop_reason
            activity = ("GOALRUN_FAILED", "FAILED")
        state.finished_at = datetime.now(UTC)
        save_goal_run(state, self.project_root)
        await self._emit_activity(
            goal_run_id,
            activity[0],
            activity[1],
            {
                "execution_id": execution_id,
                "exit_code": result.exit_code,
                "substrate_id": substrate,
                "substrate_kind": substrate_kind,
            },
            provider_id=admitted_executor,
        )
        self._notify_waiters(goal_run_id)
        # A terminal run releases its admission lease before the queue drains,
        # so the next queued run can actually acquire the freed resource.
        from server.goal_run.pre_admission import _release_admission_lease

        _release_admission_lease(state, self.project_root)
        # P6: a finished run frees its resources; admit the next queued run.
        self.drain_queue()
        return await self.status(goal_run_id)

    # ── Status ───────────────────────────────────────────────────────

    async def status(self, goal_run_id: str) -> ExecutionControlResult:
        """Get comprehensive execution status."""
        state = load_goal_run(self.project_root, goal_run_id)
        if not state:
            raise ValueError(f"GoalRun {goal_run_id} not found")

        execution_id, binding_id, executor_id = await self._get_binding(goal_run_id)
        provider_session_id, provider_run_id, _ = await self._get_binding(goal_run_id)

        phase = self._derive_phase(state)
        return ExecutionControlResult(
            goal_run_id=goal_run_id,
            phase=phase,
            goal_status=state.status,
            execution_id=execution_id,
            binding_id=binding_id,
            provider_session_id=provider_session_id,
            provider_run_id=provider_run_id,
            executor_id=executor_id,
        )

    def _derive_phase(self, state: GoalRunState) -> ExecutionControlPhase:
        """Derive ExecutionControl phase from GoalRun state."""
        mapping = {
            GoalStatus.pending_execution: ExecutionControlPhase.ADMITTED,
            GoalStatus.running: ExecutionControlPhase.RUNNING,
            GoalStatus.paused: ExecutionControlPhase.WAITING,
            GoalStatus.resumable: ExecutionControlPhase.WAITING,
            GoalStatus.recovering: ExecutionControlPhase.RECOVERY_REQUIRED,
            GoalStatus.completed: ExecutionControlPhase.COMPLETED,
            GoalStatus.recovered: ExecutionControlPhase.COMPLETED,
            GoalStatus.partial_completed: ExecutionControlPhase.COMPLETED,
            GoalStatus.failed: ExecutionControlPhase.FAILED,
            GoalStatus.blocked: ExecutionControlPhase.RECOVERY_REQUIRED,
            GoalStatus.cancelled: ExecutionControlPhase.CANCELLED,
        }
        return mapping.get(state.status, ExecutionControlPhase.CREATED)

    # ── Wait ─────────────────────────────────────────────────────────

    async def wait(self, goal_run_id: str, timeout: float | None = None) -> ExecutionControlResult:
        """Wait for execution to reach terminal state."""
        start = time.time()
        while True:
            result = await self.status(goal_run_id)
            if result.goal_status in (
                GoalStatus.completed,
                GoalStatus.recovered,
                GoalStatus.partial_completed,
                GoalStatus.failed,
                GoalStatus.cancelled,
            ):
                return result

            if timeout and (time.time() - start) > timeout:
                raise TimeoutError(f"Wait timeout for {goal_run_id}")

            # Wait for state change event
            event = self._running_waiters.get(goal_run_id)
            if not event:
                event = asyncio.Event()
                self._running_waiters[goal_run_id] = event

            try:
                await asyncio.wait_for(event.wait(), timeout=5.0)
            except TimeoutError:
                pass
            finally:
                self._running_waiters.pop(goal_run_id, None)

    def _notify_waiters(self, goal_run_id: str) -> None:
        """Notify waiters of state change."""
        event = self._running_waiters.get(goal_run_id)
        if event:
            event.set()

    # ── Stream ───────────────────────────────────────────────────────

    async def stream(
        self, goal_run_id: str, cursor: int | None = None
    ) -> tuple[list[ActivityEvent], int]:
        """Stream execution activity events from cursor (P7 durable cursor)."""
        activities, next_cursor = self._recorder.stream(goal_run_id, cursor=cursor)
        events = [
            ActivityEvent(
                sequence=a.sequence,
                goal_run_id=goal_run_id,
                event_type=a.type,
                phase=a.phase,
                payload=a.payload,
                provider_id=a.provider_id,
                provider_run_id=a.provider_run_id,
                timestamp=a.timestamp,
            )
            for a in activities
        ]
        return events, next_cursor

    # ── Steer ────────────────────────────────────────────────────────

    async def steer(self, goal_run_id: str, instruction: str) -> ExecutionControlResult:
        """Send steering instruction to running execution."""
        state = load_goal_run(self.project_root, goal_run_id)
        if not state:
            raise ValueError(f"GoalRun {goal_run_id} not found")

        if state.status != GoalStatus.running:
            raise RuntimeError(f"GoalRun {goal_run_id} not running (status: {state.status.value})")

        # Emit activity
        await self._emit_activity(
            goal_run_id,
            "STEER_REQUESTED",
            "STEERING",
            {"instruction": instruction},
            provider_id=state.requested_executor,
        )

        # For now, record the steer instruction in GoalRun
        state.unfinished_work.append(f"steer:{instruction}")
        save_goal_run(state, self.project_root)

        # TODO: Actually forward to provider adapter via executor binding
        # This requires integration with the provider adapter layer

        return await self.status(goal_run_id)

    # ── Cancel ───────────────────────────────────────────────────────

    async def cancel(self, goal_run_id: str) -> ExecutionControlResult:
        """Cancel execution idempotently."""
        state = load_goal_run(self.project_root, goal_run_id)
        if not state:
            raise ValueError(f"GoalRun {goal_run_id} not found")

        if state.status in (
            GoalStatus.completed,
            GoalStatus.recovered,
            GoalStatus.partial_completed,
            GoalStatus.cancelled,
        ):
            # Already terminal - idempotent
            return await self.status(goal_run_id)

        execution_id, binding_id, executor_id = await self._get_binding(goal_run_id)

        # Emit activity
        await self._emit_activity(
            goal_run_id,
            "CANCEL_REQUESTED",
            "CANCELLING",
            {"execution_id": execution_id},
            provider_id=executor_id,
        )

        # Update GoalRun
        state.status = GoalStatus.cancelled
        state.finished_at = datetime.now(UTC)
        save_goal_run(state, self.project_root)

        # Release admission lease
        from server.goal_run.pre_admission import _release_admission_lease

        _release_admission_lease(state, self.project_root)

        # TODO: Call provider adapter cancel via executor binding
        # For now, mark binding as cancelled
        if binding_id:
            self._update_binding_status(binding_id, BindingStatus.COMPLETED)

        await self._emit_activity(
            goal_run_id,
            "CANCEL_COMPLETED",
            "CANCELLED",
            {"execution_id": execution_id},
            provider_id=executor_id,
        )

        self._notify_waiters(goal_run_id)
        # P6: cancellation frees resources; admit the next queued run.
        self.drain_queue()
        return await self.status(goal_run_id)

    def _update_binding_status(self, binding_id: str, status: BindingStatus) -> None:
        """Update binding status in persisted record."""
        import json

        from server.goal_run.execution_admission import _admission_dir

        binding_path = _admission_dir(self.project_root) / f"binding_{binding_id}.json"
        if not binding_path.exists():
            return
        try:
            data = json.loads(binding_path.read_text(encoding="utf-8"))
            data["status"] = status.value
            data["updated_at"] = datetime.now(UTC).isoformat()
            binding_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except Exception as exc:
            logger.warning(f"Failed to update binding status: {exc}")

    # ── Pause / Resume ───────────────────────────────────────────────

    async def pause(self, goal_run_id: str) -> ExecutionControlResult:
        """Pause execution if provider supports it."""
        state = load_goal_run(self.project_root, goal_run_id)
        if not state:
            raise ValueError(f"GoalRun {goal_run_id} not found")

        if state.status != GoalStatus.running:
            raise RuntimeError(
                f"Cannot pause: GoalRun {goal_run_id} status is {state.status.value}"
            )

        await self._emit_activity(
            goal_run_id,
            "PAUSE_REQUESTED",
            "PAUSING",
            {},
            provider_id=state.requested_executor,
        )

        state.status = GoalStatus.paused
        save_goal_run(state, self.project_root)

        return await self.status(goal_run_id)

    async def resume(self, goal_run_id: str) -> ExecutionControlResult:
        """Resume paused execution."""
        state = load_goal_run(self.project_root, goal_run_id)
        if not state:
            raise ValueError(f"GoalRun {goal_run_id} not found")

        if state.status != GoalStatus.paused:
            raise RuntimeError(
                f"Cannot resume: GoalRun {goal_run_id} status is {state.status.value}"
            )

        await self._emit_activity(
            goal_run_id,
            "RESUME_REQUESTED",
            "RESUMING",
            {},
            provider_id=state.requested_executor,
        )

        state.status = GoalStatus.running
        save_goal_run(state, self.project_root)

        return await self.status(goal_run_id)

    # ── Reconcile ────────────────────────────────────────────────────

    async def reconcile(self, goal_run_id: str) -> ExecutionControlResult:
        """Reconcile execution state after restart/crash."""
        state = load_goal_run(self.project_root, goal_run_id)
        if not state:
            raise ValueError(f"GoalRun {goal_run_id} not found")

        await self._emit_activity(
            goal_run_id,
            "RECOVERY_STARTED",
            "RECOVERY_REQUIRED",
            {"reason": "reconcile"},
            provider_id=state.requested_executor,
        )

        # Check if provider is reachable
        execution_id, binding_id, executor_id = await self._get_binding(goal_run_id)
        provider_ok = False

        if executor_id:
            try:
                from veya.remote.executor_registry import get_executor_registry

                identity = get_executor_registry().identity(executor_id)
                if identity.selectable and identity.availability not in (
                    "UNAVAILABLE",
                    "DISABLED",
                    "LEGACY",
                ):
                    provider_ok = True
            except Exception:
                pass

        if not provider_ok:
            # Provider lost - mark as recovery required
            state.status = GoalStatus.recovering
            save_goal_run(state, self.project_root)

            await self._emit_activity(
                goal_run_id,
                "RECOVERY_FAILED",
                "RECOVERY_REQUIRED",
                {"reason": "provider_unavailable"},
                provider_id=executor_id,
            )
            return ExecutionControlResult(
                goal_run_id=goal_run_id,
                phase=ExecutionControlPhase.RECOVERY_REQUIRED,
                goal_status=state.status,
                execution_id=execution_id,
                binding_id=binding_id,
                reason="Provider unavailable",
            )

        # Provider reachable - check if execution still running
        # For now, assume we can resume
        state.status = GoalStatus.running
        save_goal_run(state, self.project_root)

        await self._emit_activity(
            goal_run_id,
            "RECOVERY_COMPLETED",
            "RUNNING",
            {"execution_id": execution_id},
            provider_id=executor_id,
        )

        return await self.status(goal_run_id)

    # ── Resource queue (P6) ─────────────────────────────────────────

    def drain_queue(self) -> list[dict[str, Any]]:
        """Admit queued executions FIFO whose resources are now free (P6).

        Called after a terminal transition releases the workspace. Queue state
        lives in the existing admission records; no second scheduler.
        """
        try:
            admitted = self.admission.admit_queued(self.project_root)
        except Exception as exc:  # fail-open: a queue fault must not fail a run
            logger.warning("queue drain failed: %s", exc)
            return []
        return [{"goal_run_id": r.goal_run_id, "admission_id": r.admission_id} for r in admitted]

    def queued(self) -> list[dict[str, Any]]:
        """FIFO view of executions waiting for a resource (WAITING_FOR_RESOURCE)."""
        try:
            return self.admission.queued_admissions(self.project_root)
        except Exception:
            return []


def get_execution_control(project_root: str) -> ExecutionControl:
    """Get or create ExecutionControl for a project root."""
    return ExecutionControl(project_root)
