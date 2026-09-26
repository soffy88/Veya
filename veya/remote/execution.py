"""Durable execution lifecycle for the remote MCP gateway (P0-C..P0-J).

The canonical remote contract is *submit + execution_id + poll*:

    submit   -> persist execution -> start durable worker -> return immediately
    status   -> real incremental state (phase / step / events / heartbeat)
    cancel   -> explicit, idempotent cancellation

Hard invariants:

* An RPC timeout / client disconnect never cancels an execution. Only an
  explicit ``process.cancel`` does (``CLIENT_TIMEOUT_EXECUTION_SURVIVES=YES``).
* State is persisted to disk, not only process memory, so a reconnected client
  (and a restarted gateway process) can still read ``process.status``.
* Ownership is the token, not the transient session id, so reconnect works —
  while a different token is still denied (cross-user read/cancel blocked).
* Progress is real: phases and events come from the worker, never a fabricated
  percentage. Chain-of-thought is never recorded here.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import threading
import time
import uuid
from collections.abc import Awaitable, Callable
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

_logger = logging.getLogger(__name__)


def _normalize_execution_cap(value: Any) -> int | None:
    """Positive hard cap, or ``None`` for unlimited execution admission."""
    if value is None:
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


from veya.remote.execution_contract import (  # noqa: E402
    ExecutionCondition,
    ExecutionSpec,
)


class ExecutionPhase(StrEnum):
    """Canonical lifecycle (external contract, stable)."""

    QUEUED = "QUEUED"
    STARTING = "STARTING"
    WORKSPACE_VALIDATION = "WORKSPACE_VALIDATION"
    PLANNING = "PLANNING"
    RUNNING = "RUNNING"
    EDITING = "EDITING"
    TESTING = "TESTING"
    SUSPENDING = "SUSPENDING"
    SUSPENDED = "SUSPENDED"
    RESUMING = "RESUMING"
    RECOVERING = "RECOVERING"
    FINALIZING = "FINALIZING"
    COMPLETED = "COMPLETED"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    TERMINATED = "TERMINATED"


class ExecutionStatus(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    STALLED = "STALLED"
    COMPLETED = "COMPLETED"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


TERMINAL_PHASES = frozenset(
    {
        ExecutionPhase.COMPLETED,
        ExecutionPhase.BLOCKED,
        ExecutionPhase.FAILED,
        ExecutionPhase.CANCELLED,
        ExecutionPhase.TERMINATED,
    }
)
RUNNING_PHASES = (
    ExecutionPhase.QUEUED,
    ExecutionPhase.STARTING,
    ExecutionPhase.WORKSPACE_VALIDATION,
    ExecutionPhase.PLANNING,
    ExecutionPhase.EDITING,
    ExecutionPhase.TESTING,
    ExecutionPhase.RUNNING,
    ExecutionPhase.SUSPENDING,
    ExecutionPhase.SUSPENDED,
    ExecutionPhase.RESUMING,
    ExecutionPhase.RECOVERING,
    ExecutionPhase.FINALIZING,
)
_PHASE_ORDER: dict[str, int] = {str(phase): index for index, phase in enumerate(RUNNING_PHASES)}


class ExecutionType(StrEnum):
    """Which canonical execution family a durable record belongs to."""

    HICODE = "hicode"
    DIRECT = "direct"


# Direct execution has no planning/editing semantic layer (P0-D): it is a
# primitive RPC over the host.
DIRECT_PHASE_ORDER: dict[str, int] = {
    "QUEUED": 0,
    "STARTING": 1,
    "RUNNING": 2,
    "FINALIZING": 3,
    "COMPLETED": 4,
}
_DIRECT_TAIL_BYTES = 32_000
_FAILURE_DETAIL_BYTES = 4_000
_MAX_EVENTS = 40
_MAX_FAILURE_HISTORY = 20
_RAW_FAILURE_EVIDENCE_BYTES = 8_000

_LEGACY_STATE = {
    ExecutionStatus.QUEUED: "PENDING",
    ExecutionStatus.RUNNING: "RUNNING",
    ExecutionStatus.STALLED: "RUNNING",
    ExecutionStatus.COMPLETED: "SUCCEEDED",
    ExecutionStatus.BLOCKED: "FAILED",
    ExecutionStatus.FAILED: "FAILED",
    ExecutionStatus.CANCELLED: "CANCELLED",
}


class ExecutionError(Exception):
    """Stable, serializable execution error (``code`` is a remote error code)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = str(code)
        self.message = message


class ExecutionBlocked(ExecutionError):
    """Execution was refused before it could start (fail-closed)."""


_DIRECT_COMMAND_TOOLS = frozenset({"shell.exec", "test.run", "build.run"})


def _is_direct_command(record: Any) -> bool:
    """True for a direct shell/test/build command execution (P0-G).

    Only these records always report through ``finish_command`` (which sets
    ``direct_status``); CLI-worker and Hicode children share the ``direct``
    execution type but never set it, so they must not be judged here.
    """

    return (
        getattr(record, "execution_type", None) == str(ExecutionType.DIRECT)
        and str(getattr(record, "tool", "") or "") in _DIRECT_COMMAND_TOOLS
    )


def direct_spawn_failure(record: Any) -> bool:
    """True when a direct command never produced an exit code (P0-G).

    Command spawn failure, missing binary, sandbox profile error: the record
    carries ``direct_status != passed`` with ``exit_code None``. Such a
    record must terminate FAILED with taxonomy — never COMPLETED.
    """

    direct_status = getattr(record, "direct_status", None)
    command = getattr(record, "command", None)
    return (
        _is_direct_command(record)
        and (
            (direct_status is not None and direct_status != "passed")
            or (command is not None and direct_status != "passed")
        )
        and getattr(record, "exit_code", None) is None
    )


@dataclass
class ExecutionRecord:
    """Everything the gateway persists for one execution."""

    execution_id: str
    task_id: str
    session_id: str
    token_id: str
    principal: str
    tool: str
    veya_tool: str
    requested_workspace: str
    requested_realpath: str
    resolved_repo_root: str
    repo_identity: str
    worktree_path: str | None = None
    worktree_repo_root: str | None = None
    worktree_branch: str | None = None
    isolated_worktree: bool = False
    keep_worktree: bool = False
    # Execution-scoped worktree evidence.  These are projections of the
    # resource binding; ExecutionStore remains a projection, not a second
    # worktree or lifecycle authority.
    worktree_binding_key: str | None = None
    worktree_base_sha: str | None = None
    execution_commit_sha: str | None = None
    promotion_state: str | None = None
    canonical_after_sha: str | None = None
    principal_id: str | None = None
    agent_role: str = "worker"
    agent_identity: str | None = None
    status: str = str(ExecutionStatus.QUEUED)
    phase: str = str(ExecutionPhase.QUEUED)
    current_step: int = 0
    total_steps: int = 0
    message: str = ""
    conditions: list[ExecutionCondition] = field(default_factory=list)
    spec: ExecutionSpec | None = None
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    updated_at: float = field(default_factory=time.time)
    heartbeat_at: float | None = None
    last_activity_at: float | None = None
    completed_at: float | None = None
    error: str | None = None
    result_summary: str | None = None
    cancel_requested: bool = False
    worker_alive: bool = False
    events: list[dict[str, Any]] = field(default_factory=list)
    # P0-L: distinct budget fields; ``timeout_sec`` is never the submit RPC wait.
    max_steps: int | None = None
    execution_timeout_sec: float | None = None
    effective_timeout_ms: int | None = None
    command_timeout_sec: float | None = None
    heartbeat_timeout_sec: float | None = None
    # P0-D/E/F: direct execution identity and bounded streaming state.
    execution_type: str = str(ExecutionType.HICODE)
    command: str | None = None
    cwd: str | None = None
    profile: str | None = None
    stdout_tail: str = ""
    stderr_tail: str = ""
    bytes_stdout: int = 0
    bytes_stderr: int = 0
    last_output_at: float | None = None
    exit_code: int | None = None
    direct_status: str | None = None
    # P1-A/H/I: explicit LLM execution mode + worker/provider observability.
    execution_mode: str | None = None
    orchestrator: str | None = None
    worker_type: str | None = None
    worker_id: str | None = None
    model_provider: str | None = None
    model: str | None = None
    parent_execution_id: str | None = None
    model_request_count: int = 0
    tool_call_count: int = 0
    model_request_started_at: float | None = None
    model_request_completed_at: float | None = None
    last_tool_activity_at: float | None = None
    current_activity: str | None = None
    worker_workspace: str | None = None
    worker_pid: int | None = None
    active_tool_pid: int | None = None
    process_group_id: int | None = None
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    # Continuity: execution identity vs worker-runtime vs provider-context (A2).
    worker_runtime_id: str | None = None
    worker_runtime_state: str | None = None
    preferred_worker_runtime_id: str | None = None
    affinity_state: str | None = None
    context_continuity_lost: bool = False
    provider_session_id: str | None = None
    context_generation: int | None = None
    command_id: str | None = None
    command_state: str | None = None
    side_effect_state: str | None = None
    pending_commands: int = 0
    active_tool_count: int = 0
    active_process_count: int = 0
    output_settled: bool = False
    artifact_flushed: bool = False
    worker_final_claim: bool = False
    # Stage B: shared execution-capability context identity + failure taxonomy.
    execution_context_id: str | None = None
    execution_context_hash: str | None = None
    context_budget: dict[str, Any] = field(default_factory=dict)
    selected_capability_ids: list[str] = field(default_factory=list)
    selected_skill_ids: list[str] = field(default_factory=list)
    failure_class: str | None = None
    failure_source: str | None = None
    failure_detail: str | None = None
    failure_message: str | None = None
    provider_error_code: str | None = None
    raw_failure_evidence: dict[str, Any] | None = None
    failure_history: list[dict[str, Any]] = field(default_factory=list)
    round_history: list[dict[str, Any]] = field(default_factory=list)
    last_event: dict[str, Any] | None = None
    # L1 parallel dispatch: a parent only aggregates; children carry parent_execution_id.
    role: str = "worker"
    failure_mode: str = "collect_all"
    child_execution_ids: list[str] = field(default_factory=list)
    # Canonical GoalRun identity.  ExecutionStore persists this projection;
    # it is not a second durable execution state machine.
    goal_run_id: str | None = None
    goal_task_id: str | None = None
    goal_project_root: str | None = None
    last_event_cursor: int = 0
    idempotency_key: str | None = None
    task_contract: dict[str, Any] | None = None
    effect_receipt: dict[str, Any] | None = None
    finalization_status: str | None = None
    finalization_failure_class: str | None = None

    @property
    def is_direct(self) -> bool:
        return self.execution_type == str(ExecutionType.DIRECT)

    @property
    def is_terminal(self) -> bool:
        return self.phase in {str(p) for p in TERMINAL_PHASES}

    def to_public(self, *, heartbeat_timeout_s: float, now: float | None = None) -> dict[str, Any]:
        moment = time.time() if now is None else now
        status = self.status
        if (
            not self.is_terminal
            and self.heartbeat_at is not None
            and (moment - self.heartbeat_at) > heartbeat_timeout_s
        ):
            status = str(ExecutionStatus.STALLED)
        worker_alive = (
            self.worker_alive and not self.is_terminal and status != str(ExecutionStatus.STALLED)
        )
        end = self.completed_at if self.completed_at is not None else moment
        elapsed_ms = None
        if self.started_at is not None:
            elapsed_ms = round((end - self.started_at) * 1000, 3)
        heartbeat_age = None if self.heartbeat_at is None else moment - self.heartbeat_at
        if heartbeat_age is None:
            worker_heartbeat = "UNKNOWN"
        elif heartbeat_age <= heartbeat_timeout_s:
            worker_heartbeat = "HEALTHY"
        else:
            worker_heartbeat = "STALE"
        # Keep enough lifecycle evidence for a fast worker to expose both its
        # admission and provider-boundary events in one status projection.
        recent_events = list(self.events[-20:])
        # CHECKPOINT is a lifecycle boundary, not ordinary chatter.  Keep the
        # real persisted marker visible even when later model/tool events push
        # it outside the rolling ten-event window.
        if self.events and not any(event.get("kind") == "CHECKPOINT" for event in recent_events):
            checkpoint = next(
                (event for event in reversed(self.events) if event.get("kind") == "CHECKPOINT"),
                None,
            )
            if checkpoint is not None:
                recent_events.insert(0, checkpoint)
        payload: dict[str, Any] = {
            "execution_id": self.execution_id,
            "execution_type": self.execution_type,
            "task_id": self.task_id,
            "tool": self.tool,
            "veya_tool": self.veya_tool,
            # Canonical identity fields (P0-J).
            "requested_workspace": self.requested_workspace,
            "resolved_repo_root": self.resolved_repo_root,
            "repo_identity": self.repo_identity,
            "worktree": self.worktree_path,
            "worktree_repo_root": self.worktree_repo_root,
            "worktree_binding_key": self.worktree_binding_key,
            "worktree_base_sha": self.worktree_base_sha,
            "execution_commit_sha": self.execution_commit_sha,
            "promotion_state": self.promotion_state,
            "canonical_after_sha": self.canonical_after_sha,
            "task_contract": dict(self.task_contract or {}),
            "effect_receipt": dict(self.effect_receipt or {}),
            "finalization_status": self.finalization_status,
            "finalization_failure_class": self.finalization_failure_class,
            # Lifecycle (P0-D/E).
            "status": status,
            "phase": self.phase,
            "current_step": max(self.current_step, self.tool_call_count),
            "total_steps": self.total_steps or self.max_steps or None,
            "message": self.message,
            "current_activity": self.current_activity,
            "execution_mode": self.execution_mode,
            "orchestrator": self.orchestrator,
            "worker_type": self.worker_type,
            "worker_id": self.worker_id,
            "worker_workspace": self.worker_workspace,
            "worker_pid": self.worker_pid,
            "worker_runtime_id": self.worker_runtime_id,
            "worker_runtime_state": self.worker_runtime_state,
            "preferred_worker_runtime_id": self.preferred_worker_runtime_id,
            "affinity_state": self.affinity_state,
            "context_continuity_lost": self.context_continuity_lost,
            "provider_session_id": self.provider_session_id,
            "context_generation": self.context_generation,
            "command_id": self.command_id,
            "command_state": self.command_state,
            "side_effect_state": self.side_effect_state,
            "pending_commands": self.pending_commands,
            "active_tool_count": self.active_tool_count,
            "active_process_count": self.active_process_count,
            "output_settled": self.output_settled,
            "artifact_flushed": self.artifact_flushed,
            "worker_final_claim": self.worker_final_claim,
            "execution_context_id": self.execution_context_id,
            "execution_context_hash": self.execution_context_hash,
            "context_budget": dict(self.context_budget),
            "selected_capability_ids": list(self.selected_capability_ids),
            "selected_skill_ids": list(self.selected_skill_ids),
            "failure_class": self.failure_class,
            "failure_source": self.failure_source,
            "failure_detail": self.failure_detail,
            "failure_message": self.failure_message,
            "provider_error_code": (self.provider_error_code or self.error),
            "raw_failure_evidence": dict(self.raw_failure_evidence or {})
            if self.raw_failure_evidence
            else None,
            "failure_history": list(self.failure_history),
            "round_history": list(self.round_history),
            "progress": {
                "unit": "tool_calls" if self.tool_call_count else "steps",
                "current": max(self.current_step, self.tool_call_count),
                "total": self.total_steps or self.max_steps or None,
                "tool_calls": self.tool_call_count,
                "model_requests": self.model_request_count,
            },
            "last_event": dict(self.last_event or (self.events[-1] if self.events else {})),
            "active_tool_pid": self.active_tool_pid,
            "process_group_id": self.process_group_id,
            "worker_heartbeat": worker_heartbeat,
            "heartbeat_age_s": None if heartbeat_age is None else round(heartbeat_age, 3),
            "model_provider": self.model_provider,
            "model": self.model,
            "parent_execution_id": self.parent_execution_id,
            "role": self.role,
            "failure_mode": self.failure_mode,
            "child_execution_ids": list(self.child_execution_ids),
            "model_request_count": self.model_request_count,
            "tool_call_count": self.tool_call_count,
            "model_in_flight": self.model_request_started_at is not None
            and (
                self.model_request_completed_at is None
                or self.model_request_started_at > self.model_request_completed_at
            ),
            "model_request_started_at": self.model_request_started_at,
            "model_request_completed_at": self.model_request_completed_at,
            "last_tool_activity_at": self.last_tool_activity_at,
            "artifacts": list(self.artifacts),
            "created_at": self.created_at,
            "started_at": self.started_at,
            "updated_at": self.updated_at,
            "heartbeat_at": self.heartbeat_at,
            "last_activity_at": self.last_activity_at,
            "completed_at": self.completed_at,
            "elapsed_ms": elapsed_ms,
            "error": self.error,
            "result_summary": self.result_summary,
            "worker_alive": worker_alive,
            "cancel_requested": self.cancel_requested,
            "recent_events": recent_events,
            "max_steps": self.max_steps,
            "execution_timeout_sec": self.execution_timeout_sec,
            "effective_timeout_ms": self.effective_timeout_ms,
            "command_timeout_sec": self.command_timeout_sec,
            "heartbeat_timeout_sec": self.heartbeat_timeout_sec,
            # Legacy aliases kept for existing callers/tests.
            "state": (
                _LEGACY_STATE.get(ExecutionStatus(status), status)
                if status in ExecutionStatus._value2member_map_
                else status
            ),
            "workspace": self.requested_realpath,
            "session_id": self.session_id,
        }
        if self.is_direct:
            payload.update(
                {
                    "command": self.command,
                    "cwd": self.cwd,
                    "profile": self.profile,
                    "stdout_tail": self.stdout_tail,
                    "stderr_tail": self.stderr_tail,
                    "last_output": (self.stdout_tail + self.stderr_tail)[-4000:],
                    "bytes_stdout": self.bytes_stdout,
                    "bytes_stderr": self.bytes_stderr,
                    "last_output_at": self.last_output_at,
                    "exit_code": self.exit_code,
                    "output_active": self.last_output_at is not None
                    and (moment - self.last_output_at) <= max(5.0, heartbeat_timeout_s / 6),
                }
            )
        return payload

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> ExecutionRecord:
        known = {f for f in cls.__dataclass_fields__}
        clean = {k: v for k, v in payload.items() if k in known}
        return cls(**clean)


class ExecutionStore:
    """Disk-backed RemoteExecution projections (atomic JSON per job).

    ``root=None`` keeps the projection in memory only (unit tests / injected
    adapters). Production drops records under ``~/.veya/remote_executions`` so
    ``process.status`` survives a caller disconnect and a gateway restart.
    GoalRun remains the canonical business execution state; this store only
    retains remote identity, metadata, and projected events/status.
    """

    def __init__(self, root: str | Path | None = None) -> None:
        self.root = Path(root).expanduser().resolve() if root is not None else None
        self._lock = threading.RLock()
        if self.root is not None:
            self.root.mkdir(parents=True, exist_ok=True)

    @classmethod
    def from_env(cls, *, default_persistent: bool) -> ExecutionStore:
        raw = os.environ.get("VEYA_REMOTE_EXECUTION_STORE")
        if raw:
            if raw.strip().lower() == "memory":
                return cls(None)
            return cls(raw)
        if default_persistent:
            return cls(Path.home() / ".veya" / "remote_executions")
        return cls(None)

    def _path(self, execution_id: str) -> Path:
        assert self.root is not None
        return self.root / f"{execution_id}.json"

    def save(self, record: ExecutionRecord) -> None:
        if self.root is None:
            return
        with self._lock:
            target = self._path(record.execution_id)
            tmp = target.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(record.to_json(), default=str), encoding="utf-8")
            os.replace(tmp, target)

    def get(self, execution_id: str) -> ExecutionRecord | None:
        if self.root is None:
            return None
        with self._lock:
            target = self._path(execution_id)
            if not target.exists():
                return None
            try:
                payload = json.loads(target.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return None
        if not isinstance(payload, dict):
            return None
        return ExecutionRecord.from_json(payload)

    def load_all(self) -> list[ExecutionRecord]:
        if self.root is None:
            return []
        records: list[ExecutionRecord] = []
        with self._lock:
            files = sorted(self.root.glob("*.json"))
        for path in files:
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(payload, dict):
                records.append(ExecutionRecord.from_json(payload))
        return records


class ProgressReporter:
    """Truthful, operation-level progress surface for one execution.

    No chain-of-thought. A phase can only move forward; a report that would move
    it backwards is recorded as an explicit ``replan`` event instead of a fake
    regression (P0-Q).
    """

    def __init__(self, manager: DurableJobManager, execution_id: str) -> None:
        self._manager = manager
        self._execution_id = execution_id

    def event(self, message: str, *, kind: str = "activity") -> None:
        self._manager.record_event(self._execution_id, kind=kind, message=str(message))

    def phase(
        self,
        phase: ExecutionPhase | str,
        *,
        message: str | None = None,
        step: int | None = None,
        total: int | None = None,
        event: str | None = None,
    ) -> None:
        self._manager.advance_phase(
            self._execution_id,
            str(phase),
            message=message,
            step=step,
            total=total,
            event=event,
        )

    def set_worktree(self, worktree_path: str, worktree_repo_root: str) -> None:
        self._manager.set_worktree(
            self._execution_id,
            worktree_path=worktree_path,
            worktree_repo_root=worktree_repo_root,
        )

    def worker(
        self,
        *,
        execution_mode: str | None = None,
        orchestrator: str | None = None,
        worker_type: str | None = None,
        model_provider: str | None = None,
        model: str | None = None,
        activity: str | None = None,
        worker_workspace: str | None = None,
    ) -> None:
        self._manager.set_worker_identity(
            self._execution_id,
            execution_mode=execution_mode,
            orchestrator=orchestrator,
            worker_type=worker_type,
            model_provider=model_provider,
            model=model,
            current_activity=activity,
            worker_workspace=worker_workspace,
        )

    def process(
        self, *, worker_pid: int | None = None, process_group_id: int | None = None
    ) -> None:
        self._manager.set_worker_identity(
            self._execution_id,
            worker_pid=worker_pid,
            process_group_id=process_group_id,
        )

    def model_started(self, *, activity: str | None = None) -> None:
        self._manager.begin_model_request(self._execution_id, activity=activity)

    def model_completed(self, *, activity: str | None = None) -> None:
        self._manager.end_model_request(self._execution_id, activity=activity)

    def tool_activity(
        self, *, activity: str | None = None, kind: str = "TOOL_ACTIVITY", count: bool = True
    ) -> None:
        self._manager.note_tool_activity(
            self._execution_id, activity=activity, kind=kind, count=count
        )

    def continuity(self, **fields: Any) -> None:
        """Publish execution-owned process/command truth for the finish boundary."""

        self._manager.set_continuity_field(self._execution_id, **fields)

    def finalization(self, result: dict[str, Any]) -> None:
        self._manager.set_finalization(self._execution_id, result)

    def execution_context(
        self,
        *,
        context_id: str,
        context_hash: str,
        budget: dict[str, Any] | None = None,
        capability_ids: list[str] | None = None,
        skill_ids: list[str] | None = None,
    ) -> None:
        self._manager.set_execution_context(
            self._execution_id,
            context_id=context_id,
            context_hash=context_hash,
            budget=budget,
            capability_ids=capability_ids,
            skill_ids=skill_ids,
        )

    def failure(
        self,
        *,
        failure_class: str,
        source: str = "worker",
        detail: str = "",
        code: str | None = None,
        raw_evidence: dict[str, Any] | None = None,
        round_index: int | None = None,
    ) -> None:
        self._manager.set_failure(
            self._execution_id,
            failure_class=failure_class,
            source=source,
            detail=detail,
            code=code,
            raw_evidence=raw_evidence,
            round_index=round_index,
        )

    def output(self, stream: str, text: str) -> None:
        self._manager.append_output(self._execution_id, stream, text)

    def finish_command(
        self,
        *,
        exit_code: int | None,
        status: str,
        command: str | None = None,
        cwd: str | None = None,
        profile: str | None = None,
        stdout_tail: str | None = None,
        stderr_tail: str | None = None,
        bytes_stdout: int | None = None,
        bytes_stderr: int | None = None,
    ) -> None:
        self._manager.set_command_result(
            self._execution_id,
            exit_code=exit_code,
            direct_status=status,
            command=command,
            cwd=cwd,
            profile=profile,
            stdout_tail=stdout_tail,
            stderr_tail=stderr_tail,
            bytes_stdout=bytes_stdout,
            bytes_stderr=bytes_stderr,
        )


Runner = Callable[[ProgressReporter], Awaitable[str]]

_current_reporter: ContextVar[ProgressReporter | None] = ContextVar(
    "remote_progress_reporter", default=None
)


@contextmanager
def use_reporter(reporter: ProgressReporter):
    """Bind ``reporter`` so executors can publish real operation-level progress."""

    token = _current_reporter.set(reporter)
    try:
        yield reporter
    finally:
        _current_reporter.reset(token)


def report_progress(
    phase: ExecutionPhase | str | None = None,
    *,
    message: str | None = None,
    step: int | None = None,
    total: int | None = None,
    event: str | None = None,
    kind: str = "activity",
) -> None:
    """Publish progress from inside a running canonical executor (no-op if unwired)."""

    reporter = _current_reporter.get()
    if reporter is None:
        return
    if phase is not None:
        reporter.phase(phase, message=message, step=step, total=total, event=event)
        if message and event is None:
            return
    text = event or message
    if text:
        reporter.event(text, kind=kind)


class DurableJobManager:
    """Remote execution projection over the canonical GoalRun authority.

    Process-local tasks in this class only carry admission or event transport;
    GoalRun owns business execution, retry, completion, and recovery.
    """

    def __init__(
        self,
        store: ExecutionStore | None = None,
        *,
        heartbeat_interval_s: float = 5.0,
        heartbeat_timeout_s: float = 60.0,
        max_jobs: int = 512,
        max_executions: int | None = None,
        max_session_executions: int | None = None,
        max_workspace_executions: int | None = None,
        worker_registry: Any = None,
        outbox: Any = None,
        recovery_runner_factory: Callable[[ExecutionRecord], Runner] | None = None,
    ) -> None:
        self.store = store if store is not None else ExecutionStore(None)
        self.heartbeat_interval_s = max(0.01, float(heartbeat_interval_s))
        self.heartbeat_timeout_s = max(self.heartbeat_interval_s * 2, float(heartbeat_timeout_s))
        self._max_jobs = max(1, int(max_jobs))
        # ``None`` means unlimited admission.  Running durable executions must
        # not block new RPCs, status queries, or new execution submission; an
        # explicit positive integer is an operator safety valve, never a
        # scheduler for the MCP data plane.
        self._execution_limits = (
            _normalize_execution_cap(max_executions),
            _normalize_execution_cap(max_session_executions),
            _normalize_execution_cap(max_workspace_executions),
        )
        self.worker_registry = worker_registry
        self.outbox = outbox
        self.recovery_runner_factory = recovery_runner_factory
        self.recovery_failures: list[dict[str, Any]] = []
        # Durable-write failures must be observable; a swallowed projection
        # write can silently lose a terminal/recovery transition.
        self.persistence_failures: list[dict[str, Any]] = []
        self._records: dict[str, ExecutionRecord] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._monitors: dict[str, asyncio.Task[None]] = {}
        self._lock = threading.RLock()
        self._load()

    # ── persistence ─────────────────────────────────────────────────
    def _load(self) -> None:
        for record in self.store.load_all():
            self._records[record.execution_id] = record

    def unfinished_count(self) -> int:
        """Number of persisted non-terminal remote projections."""
        with self._lock:
            return sum(not record.is_terminal for record in self._records.values())

    def unfinished_records(self) -> list[ExecutionRecord]:
        """Snapshot of non-terminal projections for lifecycle decisions."""
        with self._lock:
            return [record for record in self._records.values() if not record.is_terminal]

    async def recover_unfinished(
        self,
        runner_factory: Callable[[ExecutionRecord], Runner] | None = None,
    ) -> int:
        """Re-admit existing GoalRuns after a process restart.

        Recovery never creates a new remote execution identity.  A factory is
        required because provider closures are intentionally not serialized;
        without one, records remain visible for an explicit operator retry
        instead of being replayed with an invented payload.
        """
        factory = runner_factory or self.recovery_runner_factory
        recovered = 0
        for record in list(self._records.values()):
            if record.is_terminal or not record.goal_run_id or factory is None:
                continue
            try:
                runner = factory(record)
            except Exception as exc:
                failure = {
                    "execution_id": record.execution_id,
                    "goal_run_id": record.goal_run_id,
                    "task_id": record.goal_task_id or record.task_id,
                    "session_id": record.session_id,
                    "error": f"{type(exc).__name__}: {exc}",
                }
                self.recovery_failures.append(failure)
                record.failure_class = "recovery_failure"
                record.failure_source = "remote_startup_recovery"
                record.failure_detail = failure["error"][:_FAILURE_DETAIL_BYTES]
                record.events.append(
                    {
                        "ts": time.time(),
                        "kind": "recovery_failed",
                        "phase": record.phase,
                        "message": record.failure_detail,
                    }
                )
                self._persist(record)
                continue
            with self._lock:
                task = self._tasks.get(record.execution_id)
                if task is not None and not task.done():
                    continue
                self._tasks[record.execution_id] = asyncio.create_task(
                    self._admit_goal_run(record, runner),
                    name=f"veya-remote-recovery-{record.execution_id}",
                )
            recovered += 1
        if recovered:
            await asyncio.sleep(0)
        return recovered

    async def suspend(
        self,
        execution_id: str,
        *,
        token_id: str,
        session_id: str | None = None,
        workspace_realpath: str | None = None,
        principal: str | None = None,
    ) -> ExecutionRecord:
        """Suspend an active execution, halting execution while preserving lineage."""
        record = self.status(
            execution_id,
            token_id=token_id,
            workspace_realpath=workspace_realpath,
            principal=principal,
        )
        if session_id is not None and record.session_id != session_id:
            raise ExecutionError("TOOL_DENIED", "execution belongs to another session")
        if record.is_terminal:
            return record
        if record.phase == ExecutionPhase.SUSPENDED:
            return record

        record.phase = ExecutionPhase.SUSPENDING
        record.status = str(ExecutionStatus.RUNNING)
        record.message = "suspension requested"
        self._persist(record)

        with self._lock:
            task = self._tasks.get(execution_id)
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

        record.phase = ExecutionPhase.SUSPENDED
        record.status = "SUSPENDED"
        record.message = "execution suspended"
        record.events.append(
            {
                "ts": time.time(),
                "kind": "suspended",
                "phase": str(ExecutionPhase.SUSPENDED),
                "message": "execution suspended",
            }
        )
        self._persist(record)
        return record

    async def resume(
        self,
        execution_id: str,
        *,
        token_id: str,
        runner: Runner | None = None,
        workspace_realpath: str | None = None,
        principal: str | None = None,
    ) -> ExecutionRecord:
        """Forward resume to the existing GoalRun identity, idempotently."""
        record = self.status(
            execution_id,
            token_id=token_id,
            workspace_realpath=workspace_realpath,
            principal=principal,
        )
        if record.is_terminal:
            return record
        if record.goal_run_id is None:
            raise ExecutionError("GOAL_RUN_MISSING", "remote execution has no canonical GoalRun")
        chosen = runner or (
            self.recovery_runner_factory(record) if self.recovery_runner_factory else None
        )
        if chosen is None:
            raise ExecutionError(
                "RECOVERY_RUNNER_MISSING", "provider recovery factory is unavailable"
            )
        record.phase = ExecutionPhase.RESUMING
        record.status = str(ExecutionStatus.RUNNING)
        record.message = "resuming execution"
        record.events.append(
            {
                "ts": time.time(),
                "kind": "resumed",
                "phase": str(ExecutionPhase.RESUMING),
                "message": "resumed execution on canonical lineage",
            }
        )
        self._persist(record)
        with self._lock:
            current = self._tasks.get(execution_id)
            if current is None or current.done():
                self._tasks[execution_id] = asyncio.create_task(
                    self._admit_goal_run(record, chosen),
                    name=f"veya-remote-resume-{execution_id}",
                )
        return record

    def _persist(self, record: ExecutionRecord) -> None:
        record.updated_at = time.time()
        try:
            self.store.save(record)
        except Exception as exc:
            failure = {
                "execution_id": record.execution_id,
                "phase": record.phase,
                "status": record.status,
                "error": f"{type(exc).__name__}: {exc}",
                "ts": time.time(),
            }
            self.persistence_failures.append(failure)
            del self.persistence_failures[:-20]
            _logger.exception(
                "remote execution projection persist failed for %s (phase=%s)",
                record.execution_id,
                record.phase,
            )

    # ── submit ──────────────────────────────────────────────────────
    def submit(self, **kwargs: Any) -> ExecutionRecord:
        """Atomically admit against existing execution projections.

        Terminal records consume no capacity.  There is no separate slot
        counter to leak on cancellation, failure, or reconciliation.
        Control-plane status and cancellation never acquire execution slots.
        """
        session = kwargs["session"]
        binding = kwargs["binding"]
        workspace = getattr(binding, "repo_identity", "") or getattr(
            binding, "requested_realpath", ""
        )
        with self._lock:
            key = kwargs.get("idempotency_key")
            if key:
                for record in self._records.values():
                    if (
                        record.idempotency_key == key
                        and record.session_id == session.session_id
                        and (record.repo_identity or record.requested_realpath) == workspace
                    ):
                        return record
            active = [
                record
                for record in self._records.values()
                if not record.is_terminal and record.role != "parent"
            ]
            counts = (
                len(active),
                sum(r.session_id == session.session_id for r in active),
                sum((r.repo_identity or r.requested_realpath) == workspace for r in active),
            )
            for scope, count, limit in zip(
                ("global", "session", "workspace"), counts, self._execution_limits, strict=True
            ):
                if limit is not None and count >= limit:
                    raise ExecutionError(
                        "LIMIT_EXCEEDED", f"{scope} active execution limit reached"
                    )
            return self._submit_locked(**kwargs)

    def _submit_locked(
        self,
        *,
        session: Any,
        tool: str,
        veya_tool: str,
        binding: Any,
        runner: Runner,
        task_id: str | None = None,
        limits: dict[str, Any] | None = None,
        execution_type: str = str(ExecutionType.HICODE),
        command: str | None = None,
        cwd: str | None = None,
        profile: str | None = None,
        execution_mode: str | None = None,
        orchestrator: str | None = None,
        worker_type: str | None = None,
        worker_id: str | None = None,
        model_provider: str | None = None,
        model: str | None = None,
        parent_execution_id: str | None = None,
        role: str = "worker",
        preferred_worker_runtime_id: str | None = None,
        idempotency_key: str | None = None,
        spec: ExecutionSpec | None = None,
        task_contract: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> ExecutionRecord:
        if execution_type == str(ExecutionType.DIRECT):
            execution_id = f"direct_{uuid.uuid4().hex}"
            initial_phase = "QUEUED"
        else:
            execution_id = f"ex_{uuid.uuid4().hex}"
            initial_phase = str(ExecutionPhase.QUEUED)
        worktree_path = getattr(binding, "worktree_path", None)
        worktree_repo_root = getattr(binding, "worktree_repo_root", None)
        worktree_branch = kwargs.get("worktree_branch")
        isolated_worktree = bool(kwargs.get("isolated_worktree") or kwargs.get("needs_worktree"))
        # Worktree creation belongs to execution worker initialization, after
        # the execution id exists.  Submission must only persist identity; it
        # must never create a per-tool/per-submit worktree.

        principal_id = kwargs.get("principal_id") or getattr(session, "principal", "unknown")
        agent_role = role or "worker"
        agent_identity = kwargs.get("agent_identity") or f"{agent_role}:{worker_type or 'builtin'}"

        record = ExecutionRecord(
            execution_id=execution_id,
            task_id=task_id or f"task_{uuid.uuid4().hex[:16]}",
            session_id=session.session_id,
            token_id=session.token_id,
            principal=session.principal,
            principal_id=principal_id,
            agent_role=agent_role,
            agent_identity=agent_identity,
            tool=tool,
            veya_tool=veya_tool,
            requested_workspace=getattr(binding, "requested_path", "") or "",
            requested_realpath=getattr(binding, "requested_realpath", "") or "",
            resolved_repo_root=getattr(binding, "repo_root", "") or "",
            repo_identity=getattr(binding, "repo_identity", "") or "",
            worktree_path=worktree_path,
            worktree_repo_root=worktree_repo_root,
            worktree_branch=worktree_branch,
            isolated_worktree=isolated_worktree,
            keep_worktree=bool(kwargs.get("keep_worktree", False)),
            worktree_binding_key=(
                f"{execution_id},{getattr(binding, 'repo_identity', '')}"
                if getattr(binding, "repo_identity", None)
                else None
            ),
            worktree_base_sha=getattr(binding, "base_sha", None),
            heartbeat_timeout_sec=self.heartbeat_timeout_s,
            execution_type=execution_type,
            phase=initial_phase,
            command=command,
            cwd=cwd,
            profile=profile,
            execution_mode=execution_mode,
            orchestrator=orchestrator,
            worker_type=worker_type,
            worker_id=worker_id or execution_id,
            model_provider=model_provider,
            model=model,
            parent_execution_id=parent_execution_id,
            role=role,
            preferred_worker_runtime_id=preferred_worker_runtime_id,
            idempotency_key=idempotency_key,
            spec=spec,
            task_contract=task_contract or (spec.task_contract if spec else None),
        )
        for field_name in (
            "max_steps",
            "execution_timeout_sec",
            "effective_timeout_ms",
            "command_timeout_sec",
        ):
            if limits and limits.get(field_name) is not None:
                setattr(record, field_name, limits[field_name])
        record.events.append(
            {"ts": time.time(), "kind": "submitted", "phase": record.phase, "message": "accepted"}
        )
        self._attach_continuity(
            record,
            execution_mode=execution_mode,
            execution_type=execution_type,
            preferred_worker_runtime_id=preferred_worker_runtime_id,
        )
        with self._lock:
            self._evict_locked()
            self._records[record.execution_id] = record
            self._persist(record)
            self._tasks[record.execution_id] = asyncio.create_task(
                self._admit_goal_run(record, runner),
                name=f"veya-remote-admission-{record.execution_id}",
            )
        return record

    # ── L1 parallel parent (mechanical aggregation only) ────────────
    def create_parent(
        self,
        *,
        session: Any,
        tool: str,
        binding: Any,
        execution_mode: str = "direct_multi",
        failure_mode: str = "collect_all",
        parent_execution_id: str | None = None,
    ) -> ExecutionRecord:
        """Create a parent aggregator execution with no worker of its own."""

        record = ExecutionRecord(
            execution_id=f"parent_{uuid.uuid4().hex}",
            task_id=f"task_{uuid.uuid4().hex[:16]}",
            session_id=session.session_id,
            token_id=session.token_id,
            principal=session.principal,
            tool=tool,
            veya_tool="worker_dispatch",
            requested_workspace=getattr(binding, "requested_path", "") or "",
            requested_realpath=getattr(binding, "requested_realpath", "") or "",
            resolved_repo_root=getattr(binding, "repo_root", "") or "",
            repo_identity=getattr(binding, "repo_identity", "") or "",
            worktree_path=getattr(binding, "worktree_path", None),
            worktree_repo_root=getattr(binding, "worktree_repo_root", None),
            heartbeat_timeout_sec=self.heartbeat_timeout_s,
            execution_type=str(ExecutionType.DIRECT),
            execution_mode=execution_mode,
            orchestrator="none",
            worker_type="PARALLEL",
            worker_id=f"parent_{uuid.uuid4().hex[:12]}",
            phase="DISPATCHING",
            role="parent",
            failure_mode=failure_mode,
            parent_execution_id=parent_execution_id,
            heartbeat_at=time.time(),
        )
        record.events.append(
            {"ts": time.time(), "kind": "submitted", "phase": "DISPATCHING", "message": "parent"}
        )
        with self._lock:
            self._records[record.execution_id] = record
            self._persist(record)
        return record

    def attach_child(self, parent_id: str, child_id: str) -> None:
        record = self._record_for_update(parent_id)
        if child_id not in record.child_execution_ids:
            record.child_execution_ids.append(child_id)
        record.heartbeat_at = time.time()
        self._persist(record)

    def children_of(self, parent_id: str) -> list[ExecutionRecord]:
        with self._lock:
            local = list(self._records.values())
        seen = {r.execution_id: r for r in local}
        for stored in self.store.load_all():
            seen.setdefault(stored.execution_id, stored)
        parent = seen.get(parent_id)
        parent_child_ids = set(parent.child_execution_ids) if parent else set()
        return [
            r
            for r in seen.values()
            if r.parent_execution_id == parent_id or r.execution_id in parent_child_ids
        ]

    def aggregate(self, parent: ExecutionRecord) -> dict[str, Any]:
        """Mechanical child aggregation. Never ranks or picks a winner."""

        children = self.children_of(parent.execution_id)
        children.sort(key=lambda r: r.created_at)
        counts = {
            "total": len(children),
            "queued": 0,
            "running": 0,
            "completed": 0,
            "failed": 0,
            "blocked": 0,
            "cancelled": 0,
        }
        summaries = []
        for child in children:
            public = child.to_public(heartbeat_timeout_s=self.heartbeat_timeout_s)
            status = public["status"]
            if status == "QUEUED":
                counts["queued"] += 1
            elif status in {"RUNNING", "STALLED"}:
                counts["running"] += 1
            elif status == "COMPLETED":
                counts["completed"] += 1
            elif status == "BLOCKED":
                counts["blocked"] += 1
            elif status == "CANCELLED":
                counts["cancelled"] += 1
            else:
                counts["failed"] += 1
            summaries.append(
                {
                    "execution_id": child.execution_id,
                    "worker_type": child.worker_type,
                    "execution_mode": child.execution_mode,
                    "status": status,
                    "phase": public["phase"],
                    "model": child.model,
                    "current_activity": child.current_activity,
                    "message": public["message"],
                    "heartbeat_at": child.heartbeat_at,
                    "worker_heartbeat": public["worker_heartbeat"],
                    "worker_workspace": child.worker_workspace,
                    "elapsed_ms": public["elapsed_ms"],
                    "exit_code": child.exit_code,
                    "result_summary": child.result_summary,
                    "error": public["error"],
                    "failure_class": public["failure_class"],
                    "failure_source": public["failure_source"],
                    "failure_message": public["failure_message"],
                    "failure_detail": public["failure_detail"],
                    "provider_error_code": public["provider_error_code"],
                    "last_event": public["last_event"],
                    "model_request_count": public["model_request_count"],
                    "tool_call_count": public["tool_call_count"],
                    "artifacts": list(child.artifacts),
                }
            )
        if parent.status == str(ExecutionStatus.CANCELLED):
            status, phase = "CANCELLED", "CANCELLED"
        elif counts["total"] == 0:
            status, phase = "QUEUED", "DISPATCHING"
        elif counts["running"] + counts["queued"] > 0:
            status, phase = "RUNNING", "WAITING_WORKERS"
        elif counts["completed"] == counts["total"]:
            status, phase = "COMPLETED", "COMPLETED"
        elif counts["completed"] > 0:
            status, phase = "PARTIAL_COMPLETED", "COMPLETED"
        elif counts["cancelled"] == counts["total"]:
            status, phase = "CANCELLED", "CANCELLED"
        elif counts["failed"] + counts["blocked"] == counts["total"]:
            status, phase = "FAILED", "FAILED"
        if str(parent.status) != status or str(parent.phase) != phase:
            if status in ExecutionStatus._value2member_map_:
                parent.status = ExecutionStatus(status)
            elif status == "PARTIAL_COMPLETED":
                parent.status = ExecutionStatus.FAILED
            else:
                parent.status = ExecutionStatus.FAILED
            if phase in ExecutionPhase._value2member_map_:
                parent.phase = ExecutionPhase(phase)
            elif status == "RUNNING":
                parent.phase = ExecutionPhase.FINALIZING
            elif status == "QUEUED":
                parent.phase = ExecutionPhase.QUEUED
            elif status == "CANCELLED":
                parent.phase = ExecutionPhase.CANCELLED
            elif status in {"FAILED", "BLOCKED"}:
                parent.phase = ExecutionPhase(status)
            else:
                parent.phase = ExecutionPhase.COMPLETED
            if (
                status in {"COMPLETED", "FAILED", "CANCELLED", "PARTIAL_COMPLETED"}
                and parent.completed_at is None
            ):
                parent.completed_at = time.time()
            self._persist(parent)
        return {
            "status": status,
            "phase": phase,
            "counts": counts,
            "children": summaries,
        }

    def _attach_continuity(
        self,
        record: ExecutionRecord,
        *,
        execution_mode: str | None,
        execution_type: str,
        preferred_worker_runtime_id: str | None = None,
    ) -> None:
        """Allocate a worker runtime + persist the command intent before dispatch (A1).

        ``preferred_worker_runtime_id`` is honoured exactly: missing/mismatched/
        busy yields an explicit ``AFFINITY_*`` state; fallback is explicit via
        ``CONTEXT_CONTINUITY_LOST`` (never a silent swap).
        """

        worker_key = (execution_mode or execution_type or "worker").removeprefix("direct_").lower()
        if self.worker_registry is not None:
            try:
                runtime = None
                if preferred_worker_runtime_id:
                    runtime = self.worker_registry.get(preferred_worker_runtime_id)
                    if runtime is None or runtime.worker_type != worker_key:
                        record.affinity_state = "AFFINITY_UNAVAILABLE"
                        runtime = None
                    elif runtime.current_execution_id is not None:
                        record.affinity_state = "AFFINITY_BUSY"
                        runtime = None
                    else:
                        if runtime.state == "SLEEPING" and runtime.capabilities.supports_revive:
                            runtime = self.worker_registry.revive(preferred_worker_runtime_id)
                        record.affinity_state = "AFFINITY_HONORED"
                if runtime is None:
                    runtime = self.worker_registry.allocate(
                        worker_key, workspace_identity=record.requested_realpath
                    )
                    if preferred_worker_runtime_id:
                        record.context_continuity_lost = True
                        record.affinity_state = record.affinity_state or "AFFINITY_UNAVAILABLE"
                self.worker_registry.bind_execution(runtime.worker_runtime_id, record.execution_id)
                record.worker_runtime_id = runtime.worker_runtime_id
                record.worker_runtime_state = runtime.state
                record.provider_session_id = runtime.provider_session_id
                record.context_generation = runtime.context_generation
                record.events.append(
                    {
                        "ts": time.time(),
                        "kind": "WORKER_ALLOCATED",
                        "phase": record.phase,
                        "message": runtime.worker_runtime_id,
                    }
                )
            except Exception:
                pass
        if self.outbox is not None:
            try:
                command, _created = self.outbox.create(
                    idempotency_key=record.execution_id,
                    execution_id=record.execution_id,
                    worker_runtime_id=record.worker_runtime_id or "",
                    payload={"tool": record.tool, "task_id": record.task_id},
                )
                record.command_id = command.command_id
                record.command_state = command.state
                record.side_effect_state = command.side_effect_state
            except Exception:
                pass

    def set_execution_context(
        self,
        execution_id: str,
        *,
        context_id: str,
        context_hash: str,
        budget: dict[str, Any] | None = None,
        capability_ids: list[str] | None = None,
        skill_ids: list[str] | None = None,
    ) -> None:
        """Persist the built ExecutionCapabilityContext identity + budget (B1)."""

        record = self._record_for_update(execution_id)
        record.execution_context_id = context_id
        record.execution_context_hash = context_hash
        if budget:
            record.context_budget = dict(budget)
        if capability_ids is not None:
            record.selected_capability_ids = list(capability_ids)
        if skill_ids is not None:
            record.selected_skill_ids = list(skill_ids)
        self._persist(record)

    def set_failure(
        self,
        execution_id: str,
        *,
        failure_class: str,
        source: str = "worker",
        detail: str = "",
        code: str | None = None,
        raw_evidence: dict[str, Any] | None = None,
        round_index: int | None = None,
    ) -> None:
        """Persist the first canonical failure plus bounded immutable evidence history."""

        record = self._record_for_update(execution_id)
        detail_text = str(detail)[:_FAILURE_DETAIL_BYTES]
        evidence: dict[str, Any] | None = None
        if raw_evidence:
            try:
                encoded = json.dumps(raw_evidence, ensure_ascii=False, default=str)
            except (TypeError, ValueError):
                encoded = json.dumps({"repr": repr(raw_evidence)}, ensure_ascii=False)
            if len(encoded.encode("utf-8", "replace")) > _RAW_FAILURE_EVIDENCE_BYTES:
                encoded = encoded.encode("utf-8", "replace")[-_RAW_FAILURE_EVIDENCE_BYTES:].decode(
                    "utf-8", "replace"
                )
                evidence = {"truncated": True, "tail": encoded}
            else:
                loaded = json.loads(encoded)
                evidence = loaded if isinstance(loaded, dict) else {"value": loaded}
        event = {
            "ts": time.time(),
            "failure_class": str(failure_class),
            "source": str(source),
            "detail": detail_text,
            "provider_error_code": str(code or failure_class),
            "raw_failure_evidence": evidence,
            "recovered": False,
        }
        record.failure_history.append(event)
        del record.failure_history[: max(0, len(record.failure_history) - _MAX_FAILURE_HISTORY)]
        if round_index is not None:
            record.round_history.append({**event, "round_index": int(round_index)})
            del record.round_history[: max(0, len(record.round_history) - _MAX_FAILURE_HISTORY)]
        # First error wins for the current failed attempt. Wrappers may add history,
        # but cannot replace the provider/runtime root cause.
        if record.failure_class is None:
            record.failure_class = str(failure_class)
            record.failure_source = str(source)
            record.failure_detail = detail_text
            record.failure_message = detail_text or str(failure_class)
            record.provider_error_code = str(code or failure_class)
            record.raw_failure_evidence = evidence
        self._persist(record)

    def set_continuity_field(self, execution_id: str, **fields: Any) -> None:
        record = self._record_for_update(execution_id)
        for key, value in fields.items():
            if hasattr(record, key):
                setattr(record, key, value)
        self._persist(record)

    async def _wait_quiescent(self, record: ExecutionRecord, *, timeout_s: float = 60.0) -> None:
        """Final claim is not terminal: wait for owned processes/commands to end."""

        deadline = time.time() + timeout_s
        while time.time() < deadline and not record.is_terminal:
            if record.active_process_count == 0 and record.pending_commands == 0:
                return
            await asyncio.sleep(min(0.2, self.heartbeat_interval_s))

    def _settle_continuity(self, record: ExecutionRecord, *, status: str) -> None:
        """Persist terminal command/side-effect truth + release the runtime (A2/A4)."""

        if self.outbox is not None and record.command_id:
            try:
                if status == str(ExecutionStatus.COMPLETED):
                    self.outbox.mark_completed(record.command_id)
                elif status == str(ExecutionStatus.CANCELLED):
                    self.outbox.mark_failed(record.command_id, error="cancelled")
                else:
                    self.outbox.mark_failed(record.command_id, error=record.error or status)
                command = self.outbox.get(record.command_id)
                if command is not None:
                    record.command_state = command.state
                    record.side_effect_state = command.side_effect_state
            except Exception:
                pass
        if self.worker_registry is not None and record.worker_runtime_id:
            try:
                runtime = self.worker_registry.release(record.worker_runtime_id)
                record.worker_runtime_state = runtime.state
            except Exception:
                pass

    async def _admit_goal_run(self, record: ExecutionRecord, runner: Runner) -> None:
        """Admit a remote request to GoalRun and project its response.

        The nested adapter is deliberately only a provider bridge.  It has no
        scheduler, retry loop, lease, or terminal-state authority.
        """
        from pathlib import Path

        # Source worktrees can intentionally contain uninitialized 3O gitlinks.
        # Keep L0/L1 admission available in that state; 3O-dependent GoalRun
        # paths fail at their own capability boundary instead of blocking every
        # direct command before execution starts.
        from veya import platform

        if platform.available("obase"):
            platform.load("obase")
        from server.goal_run.leaf import LeafResult
        from server.goal_run.runner import project_run_goal

        manager = self

        class RemoteGoalRunAdapter:
            verification_required = True
            skip_plan_review = True
            # Remote provider completion must not wait on the optional
            # repository-wide LLM code-review advisor.  Acceptance remains
            # owned by the GoalRun/verification boundary below.
            skip_advisory_code_review = True

            async def before_execution(self, state: Any, project_root: str) -> None:
                record.goal_run_id = state.goal_id
                record.goal_project_root = project_root
                if not isinstance(state.runtime_checkpoint, dict):
                    state.runtime_checkpoint = {}
                checkpoint = state.runtime_checkpoint.setdefault("remote_execution", {})
                checkpoint["remote_job_id"] = record.execution_id
                checkpoint["session_id"] = record.session_id
                checkpoint["task_id"] = record.goal_task_id or record.task_id
                record.status = str(ExecutionStatus.RUNNING)
                record.phase = str(ExecutionPhase.EDITING)
                record.started_at = record.started_at or time.time()
                record.heartbeat_at = time.time()
                record.worker_alive = True
                manager._persist(record)

            async def before_iteration(self, state: Any, project_root: str, task: Any) -> None:
                record.goal_task_id = task.id
                record.heartbeat_at = time.time()
                manager._persist(record)

            def checkpoint(self, state: Any, project_root: str, *, reason: str) -> None:
                record.last_event_cursor += 1
                record.events.append(
                    {
                        "ts": time.time(),
                        "kind": "goal_run_checkpoint",
                        "phase": record.phase,
                        "message": reason,
                    }
                )
                manager._persist(record)

            async def execute_semantic_task(self, state: Any, task: Any) -> LeafResult:
                reporter = ProgressReporter(manager, record.execution_id)
                try:
                    with use_reporter(reporter):
                        summary = await runner(reporter)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    if isinstance(exc, ExecutionBlocked):
                        error_code = exc.code
                        failure_class = "execution_blocked"
                        detail = exc.message[:_FAILURE_DETAIL_BYTES]
                    elif isinstance(exc, ExecutionError):
                        error_code = exc.code
                        failure_class = exc.code
                        detail = exc.message[:_FAILURE_DETAIL_BYTES]
                    else:
                        error_code = type(exc).__name__
                        failure_class = "provider_failure"
                        detail = str(exc)[:_FAILURE_DETAIL_BYTES]
                    record.error = error_code
                    if record.failure_class is None:
                        manager.set_failure(
                            record.execution_id,
                            failure_class=failure_class,
                            source="remote_execution",
                            detail=detail,
                            code=error_code,
                            raw_evidence={
                                "exception_type": type(exc).__name__,
                                "detail": detail,
                            },
                        )
                    else:
                        manager._persist(record)
                    return LeafResult(
                        status="blocked",
                        summary="",
                        block_reason=f"{type(exc).__name__}: {exc}",
                        stop_reason="provider_error",
                    )
                record.result_summary = str(summary or "")
                summary_lines = record.result_summary.splitlines()
                hicode_recovery_paused = bool(summary_lines) and (
                    summary_lines[0].strip().lower().startswith("recovery_paused:")
                    or (
                        len(summary_lines) >= 2
                        and "hicode" in summary_lines[0].lower()
                        and summary_lines[1].strip().lower().startswith("recovery_paused:")
                    )
                )
                if record.execution_type == "hicode" and hicode_recovery_paused:
                    record.error = "HICODE_RECOVERY_PAUSED"
                    record.provider_error_code = "HICODE_RECOVERY_PAUSED"
                    record.failure_class = "execution_blocked"
                    record.failure_source = "hicode_runtime"
                    record.failure_detail = "Hicode provider ended in recovery_paused before completing the requested work"
                    manager._persist(record)
                    return LeafResult(
                        status="blocked",
                        summary=record.result_summary,
                        block_reason=record.failure_detail,
                        stop_reason="recovery_paused",
                    )
                record.worker_final_claim = True
                record.heartbeat_at = time.time()
                if record.active_process_count > 0 or record.pending_commands > 0:
                    record.phase = str(ExecutionPhase.FINALIZING)
                    manager._persist(record)
                    await manager._wait_quiescent(record)
                manager._persist(record)
                if record.exit_code is not None and record.exit_code != 0:
                    return LeafResult(
                        status="blocked",
                        summary=record.result_summary,
                        block_reason=record.error or f"exit_code={record.exit_code}",
                        stop_reason="provider_error",
                    )
                if direct_spawn_failure(record):
                    # P0-G: a direct shell/test/build command that did not pass
                    # (spawn failure with exit_code None, timeout, denied) is
                    # never a completion — even when no exit code was produced.
                    return LeafResult(
                        status="blocked",
                        summary=record.result_summary,
                        block_reason=record.failure_detail
                        or record.error
                        or f"direct_status={record.direct_status or 'missing'}",
                        stop_reason="provider_error",
                    )
                task_contract = record.task_contract or {}
                task_kind = str(task_contract.get("task_kind") or "").upper()
                if task_kind in {"WRITE", "TEST", "BUILD"}:
                    finalization_status = str(record.finalization_status or "").upper()
                    if finalization_status not in {"PROMOTABLE", "PROMOTED"}:
                        failure_class = record.finalization_failure_class or "FINALIZATION_MISSING"
                        manager.set_failure(
                            record.execution_id,
                            failure_class=failure_class,
                            source="l1_finalizer",
                            detail="worker returned without a validated finalization result",
                            code=failure_class,
                            raw_evidence={"task_kind": task_kind},
                        )
                        return LeafResult(
                            status="blocked",
                            summary=record.result_summary,
                            block_reason=failure_class,
                            stop_reason="finalization_missing",
                        )
                    if (
                        str(task_contract.get("promotion_policy") or "").upper()
                        == "AUTO_AFTER_VERIFY"
                        and finalization_status != "PROMOTED"
                    ):
                        manager.set_failure(
                            record.execution_id,
                            failure_class="PROMOTION_MISSING",
                            source="l1_finalizer",
                            detail="auto-promoted task did not reach PROMOTED",
                            code="PROMOTION_MISSING",
                            raw_evidence={"task_kind": task_kind},
                        )
                        return LeafResult(
                            status="blocked",
                            summary=record.result_summary,
                            block_reason="PROMOTION_MISSING",
                            stop_reason="promotion_missing",
                        )
                return LeafResult(
                    status="completed",
                    summary=record.result_summary,
                    stop_reason="completed",
                )

        root = Path(record.resolved_repo_root or record.requested_realpath or ".")
        project_root = str(root if root.is_dir() else Path.cwd())
        record.goal_task_id = record.goal_task_id or f"remote:{record.execution_id}"
        record.goal_project_root = project_root
        manager._persist(record)
        monitor = asyncio.create_task(
            self._monitor(record), name=f"veya-remote-heartbeat-{record.execution_id}"
        )
        self._monitors[record.execution_id] = monitor
        try:
            response = await project_run_goal(
                project_root=project_root,
                goal=f"Remote execution {record.execution_id}: {record.tool}",
                tasks=[
                    {
                        "id": record.goal_task_id,
                        "title": f"Remote execution {record.execution_id}",
                        "instruction": record.command or record.tool,
                        "acceptance": ["remote provider returned a successful result"],
                        "assignee": "builtin",
                    }
                ],
                mode="act_eager",
                resume_goal_id=record.goal_run_id,
                integration_adapter=RemoteGoalRunAdapter(),
                semantic_session_id=record.session_id,
                wait=True,
            )
            record.goal_run_id = response.goal_id or record.goal_run_id
            value = getattr(response.status, "value", response.status)
            if record.cancel_requested or value == "cancelled":
                self._finish(record, str(ExecutionStatus.CANCELLED), message="execution cancelled")
            elif direct_spawn_failure(record):
                # P0-G: command spawn failure (binary not executable, sandbox
                # error, OSError before exec) carries no exit code. It must be
                # FAILED with taxonomy — never COMPLETED with exit_code null.
                self._finish(
                    record,
                    str(ExecutionStatus.FAILED),
                    message=record.failure_detail
                    or record.result_summary
                    or f"direct command did not start (direct_status={record.direct_status})",
                    error=record.failure_class or "DIRECT_COMMAND_FAILED",
                )
            elif value == "completed" and not (
                record.exit_code is not None and record.exit_code != 0
            ):
                self._finish(record, str(ExecutionStatus.COMPLETED), message="completed")
            elif value == "blocked" or record.failure_class == "execution_blocked":
                self._finish(
                    record,
                    str(ExecutionStatus.BLOCKED),
                    message=record.failure_detail
                    or response.block_reason
                    or record.error
                    or record.message
                    or "execution blocked",
                    error=response.block_reason or record.error or "EXECUTION_BLOCKED",
                )
            else:
                self._finish(
                    record,
                    str(ExecutionStatus.FAILED),
                    message=record.message or response.block_reason or "execution failed",
                    error=record.error or response.block_reason or "GOAL_RUN_FAILED",
                )
        except asyncio.CancelledError:
            # A dead process is not an explicit user cancellation.  Preserve
            # the unfinished GoalRun projection so a later admission can
            # resume the same identity.
            if not record.is_terminal:
                self._persist(record)
            raise
        except Exception as exc:
            self._finish(
                record,
                str(ExecutionStatus.FAILED),
                message=f"{type(exc).__name__}: {exc}",
                error="GOAL_RUN_ADMISSION_FAILED",
            )
        finally:
            record.worker_alive = False
            self._settle_continuity(record, status=record.status)
            self._persist(record)
            with self._lock:
                self._tasks.pop(record.execution_id, None)
                self._monitors.pop(record.execution_id, None)
            if not monitor.done():
                monitor.cancel()

    async def _monitor(self, record: ExecutionRecord) -> None:
        """Advance ``heartbeat_at`` while the worker task is alive.

        A stale heartbeat therefore means the worker itself is gone (crash /
        lost process), not merely that a long command has produced no stdout.
        """

        with contextlib.suppress(asyncio.CancelledError):
            while not record.is_terminal:
                await asyncio.sleep(self.heartbeat_interval_s)
                with self._lock:
                    task = self._tasks.get(record.execution_id)
                    if task is None or task.done():
                        break
                    record.heartbeat_at = time.time()
                    record.worker_alive = True
                    self._persist(record)

    def _finish(
        self,
        record: ExecutionRecord,
        status: str,
        *,
        message: str | None = None,
        error: str | None = None,
    ) -> None:
        record.status = status
        record.phase = status
        record.completed_at = time.time()
        record.worker_alive = False
        if message is not None:
            record.message = message
        if error is not None:
            record.error = error
        if status in {str(ExecutionStatus.BLOCKED), str(ExecutionStatus.FAILED)}:
            # Terminalization must never convert the last progress narration into
            # the public root error. Preserve an earlier typed failure verbatim.
            if record.failure_class is None:
                stable_code = str(error or status)
                explicit_detail = str(error or message or status)[:_FAILURE_DETAIL_BYTES]
                record.failure_class = stable_code
                record.failure_source = "execution"
                record.failure_detail = explicit_detail
                record.failure_message = explicit_detail
                record.provider_error_code = stable_code
        elif status == str(ExecutionStatus.COMPLETED):
            # A recovered round is historical evidence, not the current terminal state.
            if record.failure_class is not None:
                recovered_at = time.time()
                for item in record.failure_history:
                    if not item.get("recovered"):
                        item["recovered"] = True
                        item["recovered_at"] = recovered_at
                for item in record.round_history:
                    if not item.get("recovered"):
                        item["recovered"] = True
                        item["recovered_at"] = recovered_at
            if record.round_history:
                record.failure_class = None
                record.failure_source = None
                record.failure_detail = None
                record.failure_message = None
                record.provider_error_code = None
                record.raw_failure_evidence = None
                record.error = None
        terminal_event = {
            "ts": record.completed_at,
            "kind": "terminal",
            "phase": status,
            "message": str(record.message or status)[:400],
        }
        record.events.append(terminal_event)
        record.last_event = terminal_event
        self._persist(record)
        if record.parent_execution_id is None and record.worktree_path and not record.keep_worktree:
            try:
                from runtime.coding.worktree import teardown_worktree

                teardown_worktree(record.worktree_path, execution_status=str(status))
            except Exception:
                pass

    # ── progress ────────────────────────────────────────────────────
    def _record_for_update(self, execution_id: str) -> ExecutionRecord:
        with self._lock:
            record = self._records.get(execution_id)
            if record is None:
                record = self.store.get(execution_id)
                if record is not None:
                    self._records[execution_id] = record
        if record is None:
            raise ExecutionError("NOT_FOUND", "unknown execution_id")
        return record

    def _lookup(self, execution_id: str) -> ExecutionRecord | None:
        """Return the freshest record for ``execution_id``.

        A record with no live task in this manager is reloaded from the store so
        a reconnected client (or a restarted gateway) sees the persisted state.
        """

        with self._lock:
            live = execution_id in self._tasks
            record = self._records.get(execution_id)
        if not live:
            stored = self.store.get(execution_id)
            if stored is not None:
                with self._lock:
                    self._records[execution_id] = stored
                return stored
        return record

    def lookup(self, execution_id: str) -> ExecutionRecord | None:
        """Public lookup returning the freshest record or None if unknown."""
        return self._lookup(execution_id)

    def record_event(
        self, execution_id: str, *, kind: str, message: str, phase: str | None = None
    ) -> None:
        record = self._record_for_update(execution_id)
        now = time.time()
        record.last_activity_at = now
        record.events.append(
            {
                "ts": now,
                "kind": kind,
                "phase": phase or record.phase,
                "message": message,
                "step": record.current_step,
                "total": record.total_steps,
            }
        )
        del record.events[: max(0, len(record.events) - _MAX_EVENTS)]
        if not record.message or kind in {"phase", "tool", "test"}:
            record.message = message
        self._persist(record)

    def advance_phase(
        self,
        execution_id: str,
        phase: str,
        *,
        message: str | None = None,
        step: int | None = None,
        total: int | None = None,
        event: str | None = None,
    ) -> None:
        record = self._record_for_update(execution_id)
        if record.is_terminal:
            return
        target = phase
        current = record.phase
        order = DIRECT_PHASE_ORDER if record.is_direct else _PHASE_ORDER
        if target in order and current in order and order[target] < order[current]:
            # Never fake a backward lifecycle step; surface it explicitly.
            self.record_event(
                execution_id,
                kind="replan",
                message=event or f"replan requested from {current} to {target}",
                phase=current,
            )
            return
        record.phase = target
        if step is not None:
            record.current_step = max(record.current_step, int(step))
        if total is not None:
            record.total_steps = int(total)
        record.status = str(ExecutionStatus.RUNNING)
        record.heartbeat_at = time.time()
        record.last_activity_at = record.heartbeat_at
        text = message or event or target
        record.message = text
        record.events.append(
            {
                "ts": record.heartbeat_at,
                "kind": "phase",
                "phase": target,
                "message": text,
                "step": record.current_step,
                "total": record.total_steps,
            }
        )
        del record.events[: max(0, len(record.events) - _MAX_EVENTS)]
        self._persist(record)

    def set_worktree(
        self, execution_id: str, *, worktree_path: str, worktree_repo_root: str
    ) -> None:
        """Record the isolated worktree identity once it has been created (P0-B)."""

        record = self._record_for_update(execution_id)
        record.worktree_path = str(worktree_path)
        record.worktree_repo_root = str(worktree_repo_root)
        self._persist(record)

    def set_finalization(self, execution_id: str, result: dict[str, Any]) -> None:
        """Persist finalizer evidence before the terminal execution decision."""

        record = self._record_for_update(execution_id)
        record.finalization_status = str(result.get("status") or "")
        record.finalization_failure_class = result.get("failure_class")
        receipt = result.get("receipt")
        if isinstance(receipt, dict):
            record.effect_receipt = receipt
            record.execution_commit_sha = result.get("commit_sha")
            record.promotion_state = result.get("promotion_status")
            promotion = receipt.get("verification", {}).get("promotion")
            if isinstance(promotion, dict):
                record.canonical_after_sha = promotion.get("canonical_after_sha")
        self._persist(record)

    def set_worker_identity(
        self,
        execution_id: str,
        *,
        execution_mode: str | None = None,
        orchestrator: str | None = None,
        worker_type: str | None = None,
        model_provider: str | None = None,
        model: str | None = None,
        current_activity: str | None = None,
        worker_workspace: str | None = None,
        worker_pid: int | None = None,
        process_group_id: int | None = None,
        active_tool_pid: int | None = None,
    ) -> None:
        """Persist explicit execution mode / worker / provider identity (P1-A/I)."""

        record = self._record_for_update(execution_id)
        for name, value in (
            ("execution_mode", execution_mode),
            ("orchestrator", orchestrator),
            ("worker_type", worker_type),
            ("model_provider", model_provider),
            ("model", model),
            ("current_activity", current_activity),
            ("worker_workspace", worker_workspace),
            ("worker_pid", worker_pid),
            ("process_group_id", process_group_id),
            ("active_tool_pid", active_tool_pid),
        ):
            if value is not None:
                setattr(record, name, value)
        self._persist(record)

    def begin_model_request(self, execution_id: str, *, activity: str | None = None) -> None:
        record = self._record_for_update(execution_id)
        now = time.time()
        record.model_request_started_at = now
        record.model_request_count += 1
        record.last_activity_at = now
        record.message = activity or "model request in flight"
        record.current_activity = record.message
        record.events.append(
            {
                "ts": now,
                "kind": "MODEL_REQUEST_STARTED",
                "phase": record.phase,
                "message": record.message,
                "step": record.current_step,
                "total": record.total_steps,
            }
        )
        del record.events[: max(0, len(record.events) - _MAX_EVENTS)]
        self._persist(record)

    def end_model_request(self, execution_id: str, *, activity: str | None = None) -> None:
        record = self._record_for_update(execution_id)
        now = time.time()
        record.model_request_completed_at = now
        record.last_activity_at = now
        if activity:
            record.current_activity = activity
            record.message = activity
        record.events.append(
            {
                "ts": now,
                "kind": "MODEL_REQUEST_COMPLETED",
                "phase": record.phase,
                "message": activity or "model request completed",
                "step": record.current_step,
                "total": record.total_steps,
            }
        )
        del record.events[: max(0, len(record.events) - _MAX_EVENTS)]
        self._persist(record)

    def note_tool_activity(
        self,
        execution_id: str,
        *,
        activity: str | None = None,
        kind: str = "TOOL_ACTIVITY",
        count: bool = True,
    ) -> None:
        record = self._record_for_update(execution_id)
        now = time.time()
        if count:
            record.tool_call_count += 1
            # Tool calls are the only universally observable unit for L1 workers.
            # Never project 0/0 after real work has occurred.
            record.current_step = max(record.current_step, record.tool_call_count)
            if record.total_steps <= 0 and record.max_steps:
                record.total_steps = int(record.max_steps)
        record.last_tool_activity_at = now
        record.last_activity_at = now
        if activity:
            record.current_activity = activity
            record.message = activity
        record.events.append(
            {
                "ts": now,
                "kind": kind,
                "phase": record.phase,
                "message": activity or kind,
                "step": record.current_step,
                "total": record.total_steps,
            }
        )
        del record.events[: max(0, len(record.events) - _MAX_EVENTS)]
        self._persist(record)

    def append_output(
        self, execution_id: str, stream: str, text: str, *, redact: Any = None
    ) -> None:
        """Append one real stdout/stderr chunk to the bounded tail store (P0-F/G)."""

        if not text:
            return
        record = self._record_for_update(execution_id)
        if redact is not None:
            text = str(redact(text))
        now = time.time()
        if stream == "stderr":
            record.stderr_tail = (record.stderr_tail + text)[-_DIRECT_TAIL_BYTES:]
            record.bytes_stderr += len(text.encode("utf-8", "replace"))
            kind = "STDERR"
        else:
            record.stdout_tail = (record.stdout_tail + text)[-_DIRECT_TAIL_BYTES:]
            record.bytes_stdout += len(text.encode("utf-8", "replace"))
            kind = "STDOUT"
        record.last_output_at = now
        record.last_activity_at = now
        line = text.strip().splitlines()[-1] if text.strip() else ""
        if line:
            record.message = line[:400]
            record.events.append(
                {
                    "ts": now,
                    "kind": kind,
                    "phase": record.phase,
                    "message": line[:400],
                    "step": record.current_step,
                    "total": record.total_steps,
                }
            )
            del record.events[: max(0, len(record.events) - _MAX_EVENTS)]
        # Throttle persistence: in-process status sees the update immediately;
        # a reconnected process sees it within ~200ms.
        if now - record.updated_at > 0.2:
            self._persist(record)

    def set_command_result(
        self,
        execution_id: str,
        *,
        exit_code: int | None,
        direct_status: str,
        command: str | None = None,
        cwd: str | None = None,
        profile: str | None = None,
        stdout_tail: str | None = None,
        stderr_tail: str | None = None,
        bytes_stdout: int | None = None,
        bytes_stderr: int | None = None,
    ) -> None:
        """Persist the final command result (P0-E)."""

        record = self._record_for_update(execution_id)
        record.exit_code = exit_code
        record.direct_status = direct_status
        if command is not None:
            record.command = command
        if cwd is not None:
            record.cwd = cwd
        if profile is not None:
            record.profile = profile
        if stdout_tail is not None:
            record.stdout_tail = stdout_tail[-_DIRECT_TAIL_BYTES:]
        if stderr_tail is not None:
            record.stderr_tail = stderr_tail[-_DIRECT_TAIL_BYTES:]
        if bytes_stdout is not None:
            record.bytes_stdout = bytes_stdout
        if bytes_stderr is not None:
            record.bytes_stderr = bytes_stderr
        record.result_summary = f"command {direct_status} (exit_code={exit_code})"
        self._persist(record)

    # ── status / cancel ─────────────────────────────────────────────
    def status(
        self,
        execution_id: str,
        *,
        token_id: str,
        workspace_realpath: str | None = None,
        principal: str | None = None,
    ) -> ExecutionRecord:
        record = self._lookup(execution_id)
        if record is None:
            raise ExecutionError("NOT_FOUND", "unknown execution_id")
        if record.token_id != token_id:
            raise ExecutionError("TOOL_DENIED", "execution belongs to another principal")
        if principal is not None:
            allowed = {
                "system",
                "admin",
                getattr(record, "token_id", None),
                getattr(record, "principal_id", None),
                getattr(record, "principal", None),
            }
            if principal not in allowed:
                raise ExecutionError(
                    "TOOL_DENIED", f"execution belongs to another principal (caller={principal})"
                )
        if workspace_realpath:
            from .workspace_binding import canonical

            # The caller may address the execution by the workspace it
            # requested (e.g. the canonical root) while the record is keyed
            # by the resolved target (e.g. an existing worktree selected via
            # workspace_path). Accept any of the execution's own workspace
            # identities; anything else stays fail-closed.
            own_identities = {
                canonical(candidate)
                for candidate in (
                    record.requested_realpath,
                    record.resolved_repo_root,
                    record.worktree_path,
                    record.worktree_repo_root,
                )
                if candidate
            }
            if canonical(workspace_realpath) not in own_identities:
                raise ExecutionError(
                    "WORKSPACE_DENIED",
                    (
                        "workspace identity does not match the persisted execution "
                        f"(execution workspace={record.requested_realpath})"
                    ),
                )
        return record

    async def cancel(
        self,
        execution_id: str,
        *,
        token_id: str,
        session_id: str | None = None,
        workspace_realpath: str | None = None,
        principal: str | None = None,
    ) -> ExecutionRecord:
        record = self.status(
            execution_id,
            token_id=token_id,
            workspace_realpath=workspace_realpath,
            principal=principal,
        )
        if session_id is not None and record.session_id != session_id:
            raise ExecutionError("TOOL_DENIED", "execution belongs to another session")
        if record.is_terminal:
            return record  # idempotent
        # L1 parent: stop new dispatch, cancel every active child, then the parent.
        if record.child_execution_ids:
            for child_id in list(record.child_execution_ids):
                try:
                    await self.cancel(
                        child_id,
                        token_id=token_id,
                        session_id=session_id,
                        workspace_realpath=workspace_realpath,
                        principal=principal,
                    )
                except ExecutionError:
                    continue
            if not record.is_terminal:
                self._finish(record, str(ExecutionStatus.CANCELLED), message="parent cancelled")
            return record
        record.cancel_requested = True
        record.message = "cancellation requested"
        self._persist(record)
        if record.goal_run_id and record.goal_project_root:
            # Cancellation is a typed GoalRun command.  The remote projection
            # must not cancel a provider coroutine or manufacture a terminal
            # business state itself.
            from server.goal_run.runner import cancel_goal

            await cancel_goal(record.goal_project_root, record.goal_run_id)
            with self._lock:
                goal_task = self._tasks.get(execution_id)
            if goal_task is not None and not goal_task.done():
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await asyncio.wait_for(asyncio.shield(goal_task), timeout=5.0)
            record = self._lookup(execution_id) or record
            if not record.is_terminal:
                self._finish(record, str(ExecutionStatus.CANCELLED), message="execution cancelled")
            return record
        with self._lock:
            task = self._tasks.get(execution_id)
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        # Explicit cancellation must always converge the remote projection.
        # If a canonical GoalRun exists it owns the business transition and is
        # commanded above; otherwise there is no business authority left to
        # preserve, so the projection itself is finished.  Leaving a
        # goal_run_id-less record non-terminal (e.g. an orphan L1 parent whose
        # process died during dispatch) would leak a stale projection that no
        # operator retry can ever clear.
        if not record.is_terminal:
            self._finish(record, str(ExecutionStatus.CANCELLED), message="execution cancelled")
        return record

    async def wait(self, execution_id: str, *, timeout_s: float | None) -> None:
        with self._lock:
            task = self._tasks.get(execution_id)
        if task is None:
            return
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=timeout_s)
        except TimeoutError:
            return

    def list_for_token(self, token_id: str) -> list[dict[str, Any]]:
        with self._lock:
            records = [r for r in self._records.values() if r.token_id == token_id]
        return [r.to_public(heartbeat_timeout_s=self.heartbeat_timeout_s) for r in records]

    def _evict_locked(self) -> None:
        if len(self._records) < self._max_jobs:
            return
        finished = [r for r in self._records.values() if r.is_terminal]
        for record in sorted(finished, key=lambda r: r.created_at)[: max(1, len(finished) // 2)]:
            self._records.pop(record.execution_id, None)


__all__ = [
    "DIRECT_PHASE_ORDER",
    "DurableJobManager",
    "ExecutionBlocked",
    "ExecutionError",
    "ExecutionPhase",
    "ExecutionRecord",
    "ExecutionStatus",
    "ExecutionStore",
    "ExecutionType",
    "ProgressReporter",
    "Runner",
    "direct_spawn_failure",
    "report_progress",
    "use_reporter",
]
