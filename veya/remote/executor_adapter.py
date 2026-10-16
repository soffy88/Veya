"""Unified ExecutorAdapter interface (P4).

All L1 executors implement this interface. The RemoteToolAdapter uses
ExecutorAdapter instances for dispatch instead of hardcoded per-worker logic.

ExecutorAdapter does NOT introduce a second TaskManager or durable authority.
All durable state remains in GoalRun / ExecutorBinding / ExecutionAdmission.
"""

from __future__ import annotations

import contextlib
import logging
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from server.events import EventEnvelope, event_store
from server.goal_run.execution_admission import get_admission
from server.goal_run.models import GoalStatus
from server.goal_run.store import load_goal_run, save_goal_run

logger = logging.getLogger("veya.executor_adapter")


class ExecutorCapability(StrEnum):
    """Capabilities an executor may declare."""

    FILESYSTEM = "filesystem"
    SHELL = "shell"
    TEST = "test"
    BUILD = "build"
    GIT = "git"
    WORKTREE = "worktree"
    NETWORK = "network"
    INTERACTIVE = "interactive"
    STREAMING = "streaming"
    STEER = "steer"
    CANCEL = "cancel"
    PAUSE = "pause"
    RESUME = "resume"
    LONG_RUNNING = "long_running"
    SESSION = "session"
    PARALLEL = "parallel"


class ExecutorHealth(StrEnum):
    """Health states for executor."""

    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    UNAVAILABLE = "UNAVAILABLE"
    DISABLED = "DISABLED"
    UNKNOWN = "UNKNOWN"
    LOST = "LOST"


@dataclass(frozen=True)
class ExecutorIdentity:
    """Static executor identity."""

    executor_id: str
    executor_kind: str
    provider: str | None = None
    model: str | None = None
    version: str | None = None


@dataclass(frozen=True)
class ExecutorCapabilities:
    """Executor capability profile."""

    supported_operations: frozenset[str] = frozenset()
    capabilities: frozenset[ExecutorCapability] = frozenset()
    write_qualified: bool = False

    @property
    def selectable(self) -> bool:
        return bool(self.supported_operations or self.capabilities)


@dataclass
class ExecutorRuntimeState:
    """Runtime state projection."""

    availability: str = "UNKNOWN"
    health: ExecutorHealth = ExecutorHealth.UNKNOWN
    failure_state: str | None = None
    last_seen: float = 0.0
    authenticated: bool = False
    reachable: bool = False
    credential_valid: bool | None = None
    credential_evidence: str = "UNPROBED"


@dataclass
class ProviderIdentity:
    """Provider identity for the executor."""

    provider_id: str
    provider_session_id: str | None = None
    provider_run_id: str | None = None


@dataclass
class AdapterStartRequest:
    """Request to start an execution."""

    goal_run_id: str
    goal_task_id: str
    execution_id: str
    task_text: str
    worktree_path: str
    repo_root: str
    task_contract: dict[str, Any] = field(default_factory=dict)
    capability_ids: list[str] = field(default_factory=list)
    permission_policy: dict[str, Any] = field(default_factory=dict)
    timeout_s: float = 600.0
    env: dict[str, str] = field(default_factory=dict)


@dataclass
class AdapterResult:
    """Result of an execution."""

    exit_code: int
    stdout: str
    stderr: str
    duration_ms: float
    artifacts: list[str] = field(default_factory=list)
    effect_receipt: dict[str, Any] | None = None
    failure_class: str | None = None
    detail: str | None = None


#: Event kinds that map directly onto a P7 activity type when reported through
#: ``ProgressReporter.event(message, kind=...)``.
_P7_EVENT_KINDS = frozenset(
    {
        "TEST_STARTED",
        "TEST_COMPLETED",
        "BUILD_STARTED",
        "BUILD_COMPLETED",
        "VERIFICATION_STARTED",
        "VERIFICATION_COMPLETED",
        "TOOL_STARTED",
        "TOOL_COMPLETED",
        "STEER_REQUESTED",
        "STEER_COMPLETED",
        "CANCEL_REQUESTED",
        "CANCEL_COMPLETED",
        "PAUSE_REQUESTED",
        "PAUSE_COMPLETED",
        "RESUME_REQUESTED",
        "RESUME_COMPLETED",
    }
)


class ProgressReporter:
    """Minimal progress reporter interface for adapters.

    When a P7 ``recorder`` is supplied, adapter progress is also written to the
    unified execution activity stream (bounded output, monotonic sequence).
    """

    def __init__(
        self,
        execution_id: str,
        goal_run_id: str,
        executor_id: str = "",
        *,
        recorder: Any | None = None,
        binding_id: str | None = None,
        provider_id: str | None = None,
        provider_run_id: str | None = None,
    ) -> None:
        self.execution_id = execution_id
        self.goal_run_id = goal_run_id
        self.executor_id = executor_id
        self._sequence = 0
        self._recorder = recorder
        self._binding_id = binding_id
        self._provider_id = provider_id or (executor_id or None)
        self._provider_run_id = provider_run_id or execution_id

    def phase(self, phase: str, message: str = "", event: str = "") -> None:
        self._emit("PHASE", {"phase": phase, "message": message, "event": event})
        self._record(
            "PROCESS_STARTED" if phase in ("STARTING",) else None,
            phase,
            {
                "message": message,
            },
        )

    def output(self, name: str, text: str) -> None:
        self._emit("OUTPUT", {"stream": name, "text": text})
        if self._recorder is not None:
            self._recorder.output(self.goal_run_id, name, text)

    def worker(self, **kwargs: Any) -> None:
        self._emit("WORKER", kwargs)

    def model_started(self, activity: str = "") -> None:
        self._emit("MODEL_STARTED", {"activity": activity})

    def model_completed(self, activity: str = "") -> None:
        self._emit("MODEL_COMPLETED", {"activity": activity})

    def process(self, **kwargs: Any) -> None:
        self._emit("PROCESS", kwargs)

    def event(self, message: str, kind: str = "") -> None:
        self._emit("EVENT", {"message": message, "kind": kind})
        self._record(kind if kind in _P7_EVENT_KINDS else None, "RUNNING", {"message": message})

    def tool_activity(self, activity: str, kind: str = "", count: bool = False) -> None:
        self._emit("TOOL_ACTIVITY", {"activity": activity, "kind": kind, "count": count})
        mapped = {
            "TOOL_STARTED": "TOOL_STARTED",
            "TOOL_COMPLETED": "TOOL_COMPLETED",
            "ARTIFACT_CREATED": "TOOL_COMPLETED",
        }.get(kind)
        self._record(mapped, "RUNNING", {"activity": activity})

    def finalization(self, data: dict[str, Any]) -> None:
        self._emit("FINALIZATION", data)

    def failure(
        self, failure_class: str, source: str, detail: str, code: str | None = None
    ) -> None:
        self._emit(
            "FAILURE",
            {
                "failure_class": failure_class,
                "source": source,
                "detail": detail,
                "code": code,
            },
        )
        self._record("TOOL_FAILED", "FAILED", {"failure_class": failure_class, "detail": detail})

    def admission(self, admission_status: str) -> None:
        self._emit("ADMISSION", {"status": admission_status})

    def set_worktree(self, worktree: str, verified_repo: str) -> None:
        self._emit("WORKTREE", {"worktree": worktree, "repo": verified_repo})

    def set_target(self, execution_target: str, dirty: bool) -> None:
        self._emit("TARGET", {"execution_target": execution_target, "dirty": dirty})

    def _record(self, p7_type: str | None, phase: str, payload: dict[str, Any]) -> None:
        if self._recorder is None or p7_type is None:
            return
        # Evidence is best-effort; never fail the execution on a stream fault.
        with contextlib.suppress(Exception):
            self._recorder.record(
                self.goal_run_id,
                p7_type,
                phase,
                payload,
                binding_id=self._binding_id,
                provider_id=self._provider_id,
                provider_run_id=self._provider_run_id,
            )

    def _emit(self, event_type: str, payload: dict[str, Any]) -> None:
        self._sequence += 1
        envelope = EventEnvelope(
            event_id=f"exec_{self.execution_id}_{self._sequence}_{datetime.now(UTC).isoformat()}",
            trace_id=self.goal_run_id,
            session_id="executor_adapter",
            task_id=None,
            turn_id=None,
            topic=f"execution.adapter.{event_type.lower()}",
            ts=datetime.now(UTC).timestamp(),
            actor=self.executor_id,
            payload={
                "goal_run_id": self.goal_run_id,
                "execution_id": self.execution_id,
                "event_type": event_type,
                "payload": payload,
            },
            schema_version=1,
        )
        event_store.append(envelope)


class ExecutorAdapter(ABC):
    """Unified executor adapter interface.

    All L1 executors (Local2, OpenCode, Claude Code, Codex, Pi, Grok, DSH, ACP)
    implement this interface. The RemoteToolAdapter uses these adapters for
    dispatch instead of hardcoded per-worker logic.

    This interface does NOT create a second TaskManager or durable authority.
    All durable state remains in GoalRun / ExecutorBinding / ExecutionAdmission.
    """

    def __init__(self, executor_id: str, project_root: str) -> None:
        self.executor_id = executor_id
        self.project_root = project_root
        self.admission = get_admission(project_root)

    # ── Identity & Capabilities ────────────────────────────────────

    @property
    @abstractmethod
    def identity(self) -> ExecutorIdentity:
        """Static executor identity."""

    @property
    @abstractmethod
    def capabilities(self) -> ExecutorCapabilities:
        """Executor capability profile."""

    @property
    @abstractmethod
    def health(self) -> ExecutorRuntimeState:
        """Current runtime state projection."""

    # ── Lifecycle ────────────────────────────────────────────────

    @abstractmethod
    async def start(
        self, request: AdapterStartRequest, reporter: ProgressReporter
    ) -> AdapterResult:
        """Start a new execution.

        The adapter owns the process lifecycle:
        - Prepare worktree / session
        - Launch provider process
        - Stream stdout/stderr to reporter
        - Return result with exit code and artifacts

        Must call reporter.phase/output/etc for activity streaming.
        """

    @abstractmethod
    async def resume(
        self, execution_id: str, provider_run_id: str, reporter: ProgressReporter
    ) -> AdapterResult:
        """Resume a provider run.

        For executors supporting session reuse (e.g. provider session).
        """

    @abstractmethod
    async def wait(
        self, execution_id: str, provider_run_id: str, timeout: float | None = None
    ) -> AdapterResult:
        """Wait for execution to reach terminal state."""

    @abstractmethod
    async def stream(
        self,
        execution_id: str,
        provider_run_id: str,
        cursor: int | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        """Stream execution activity from cursor."""

    @abstractmethod
    async def status(self, execution_id: str, provider_run_id: str) -> dict[str, Any]:
        """Get execution status."""

    @abstractmethod
    async def cancel(self, execution_id: str, provider_run_id: str) -> bool:
        """Cancel execution idempotently."""

    async def pause(self, execution_id: str, provider_run_id: str) -> bool:
        """Pause execution if supported."""
        return False

    async def steer(self, execution_id: str, provider_run_id: str, instruction: str) -> bool:
        """Send steering instruction to running execution."""
        return False

    async def pause_supported(self) -> bool:
        return ExecutorCapability.PAUSE in self.capabilities.capabilities

    async def steer_supported(self) -> bool:
        return ExecutorCapability.STEER in self.capabilities.capabilities

    async def cancel_supported(self) -> bool:
        return ExecutorCapability.CANCEL in self.capabilities.capabilities

    # ── Health & Provider Identity ──────────────────────────────

    @abstractmethod
    async def probe_health(self) -> ExecutorHealth:
        """Probe executor health. Returns current health state."""

    async def get_provider_identity(self, execution_id: str) -> ProviderIdentity | None:
        """Get provider session/run identity for an execution."""
        return None

    # ── GoalRun Integration ─────────────────────────────────────

    async def _update_goal_run_status(self, goal_run_id: str, status: GoalStatus) -> None:
        """Update GoalRun status (helper)."""
        state = load_goal_run(self.project_root, goal_run_id)
        if state:
            state.status = status
            if status == GoalStatus.running and not state.started_at:
                state.started_at = datetime.now(UTC)
            elif status in (
                GoalStatus.completed,
                GoalStatus.recovered,
                GoalStatus.failed,
                GoalStatus.cancelled,
            ):
                state.finished_at = datetime.now(UTC)
            save_goal_run(state, self.project_root)

    async def _emit_activity(
        self,
        goal_run_id: str,
        event_type: str,
        phase: str,
        payload: dict[str, Any],
    ) -> None:
        """Emit execution activity event."""
        envelope = EventEnvelope(
            event_id=f"exec_{goal_run_id}_{uuid.uuid4().hex[:8]}",
            trace_id=goal_run_id,
            session_id="executor_adapter",
            task_id=None,
            turn_id=None,
            topic=f"execution.adapter.{event_type.lower()}",
            ts=datetime.now(UTC).timestamp(),
            actor=self.executor_id,
            payload={
                "goal_run_id": goal_run_id,
                "event_type": event_type,
                "phase": phase,
                "payload": payload,
            },
            schema_version=1,
        )
        event_store.append(envelope)


# ── Adapter Registry ────────────────────────────────────────────

_ADAPTER_REGISTRY: dict[str, type[ExecutorAdapter]] = {}


def register_executor_adapter(executor_id: str, adapter_class: type[ExecutorAdapter]) -> None:
    """Register an executor adapter class."""
    _ADAPTER_REGISTRY[executor_id] = adapter_class


def unregister_executor_adapter(executor_id: str) -> None:
    """Remove an optional adapter from the registry."""
    _ADAPTER_REGISTRY.pop(executor_id, None)


def get_executor_adapter(executor_id: str, project_root: str) -> ExecutorAdapter | None:
    """Get executor adapter instance."""
    adapter_class = _ADAPTER_REGISTRY.get(executor_id)
    if adapter_class is None:
        return None
    return adapter_class(executor_id, project_root)


def list_executor_adapters() -> list[str]:
    """List registered executor IDs."""
    return list(_ADAPTER_REGISTRY.keys())
