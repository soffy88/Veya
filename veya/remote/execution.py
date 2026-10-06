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

from runtime.execution.checkpoint import CheckpointError, ExecutionCheckpointStore
from veya.remote.admission import AdmissionStatus
from veya.remote.execution_contract import ExecutionCondition, ExecutionSpec
from veya.remote.execution_events import ExecutionEventStore, ExecutionEventType

#: ``TimeoutKind`` now lives beside the timeout policy that gives it meaning, and
#: is re-exported here because the phase tables below and ``tool_adapter`` both
#: reach for it from this module.
from veya.remote.execution_timeout import (
    ExecutionPolicy,
    ExecutionTimeoutAttribution,
    ExecutionTimeoutPolicy,
    TimeoutKind,
)
from veya.remote.provider_request import ProviderRequest, ProviderRequestStatus
from veya.remote.qualification_faults import QualificationFault
from veya.remote.qualification_faults import checkpoint as qualification_checkpoint

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



#: SF-RECEIPT. A receipt is a mapping carrying every EffectReceipt field that has
#: no default. Anything else is not a receipt, however plausible it looks, and
#: accepting a partial one would let a receipt claim less than the evidence it
#: is supposed to carry (P0-K falsifiability).
_RECEIPT_REQUIRED_FIELDS: tuple[str, ...] = (
    "execution_id",
    "worker_type",
    "repo_identity",
    "worktree_path",
    "task_kind",
)


def _receipt_contract_problem(receipt: Any) -> str | None:
    """Return why ``receipt`` is not a valid receipt, or None if it is one.

    Explicit and decidable on purpose: the caller can tell a stringified receipt
    from a structurally incomplete one, and both from a genuinely absent receipt.
    """

    if not isinstance(receipt, dict):
        return f"expected a receipt object, got {type(receipt).__name__}"
    missing = [name for name in _RECEIPT_REQUIRED_FIELDS if not receipt.get(name)]
    if missing:
        return f"receipt is missing required fields: {sorted(missing)}"
    return None


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
    CHECKPOINTING = "CHECKPOINTING"
    PAUSED = "PAUSED"
    RESUMABLE = "RESUMABLE"
    RESUMING = "RESUMING"
    RECOVERING = "RECOVERING"
    COMPLETING = "COMPLETING"
    FINALIZING = "FINALIZING"
    COMPLETED = "COMPLETED"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    TIMED_OUT = "TIMED_OUT"
    TERMINATED = "TERMINATED"
    #: Admission refused the request before any worker started. Distinct from
    #: FAILED: nothing ran, so there is no runtime failure to report.
    REJECTED = "REJECTED"
    #: A cancel was accepted and is being carried out. The record is not
    #: CANCELLED until the worker has actually stopped.
    CANCEL_REQUESTED = "CANCEL_REQUESTED"
    #: An interrupted execution was re-admitted and finished successfully.
    RECOVERED = "RECOVERED"


class ExecutionStatus(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    STALLED = "STALLED"
    COMPLETED = "COMPLETED"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    TIMED_OUT = "TIMED_OUT"


TERMINAL_PHASES = frozenset(
    {
        ExecutionPhase.COMPLETED,
        ExecutionPhase.BLOCKED,
        ExecutionPhase.FAILED,
        ExecutionPhase.CANCELLED,
        ExecutionPhase.TIMED_OUT,
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
    ExecutionPhase.CHECKPOINTING,
    ExecutionPhase.PAUSED,
    ExecutionPhase.RESUMABLE,
    ExecutionPhase.COMPLETING,
    ExecutionPhase.FINALIZING,
)
_PHASE_ORDER: dict[str, int] = {str(phase): index for index, phase in enumerate(RUNNING_PHASES)}

#: States from which no further transition is legal. A finished execution is a
#: fact; it cannot later become a failure because a late writer disagreed.
TERMINAL_STATUSES: frozenset[str] = frozenset(
    {
        str(ExecutionStatus.COMPLETED),
        str(ExecutionStatus.FAILED),
        str(ExecutionStatus.BLOCKED),
        str(ExecutionStatus.CANCELLED),
        str(ExecutionStatus.TIMED_OUT),
        str(ExecutionPhase.TERMINATED),
        str(ExecutionPhase.REJECTED),
    }
)

#: Legal successors per status. Absence of a key means "no transition out".
#: RECOVERING is the single sanctioned escape hatch from a terminal failure, and
#: it is only reachable through the recovery path, never from an arbitrary
#: late writer.
_LEGAL_TRANSITIONS: dict[str, frozenset[str]] = {
    str(ExecutionStatus.QUEUED): frozenset(
        {
            str(ExecutionStatus.RUNNING),
            str(ExecutionStatus.COMPLETED),
            str(ExecutionStatus.BLOCKED),
            str(ExecutionStatus.FAILED),
            str(ExecutionStatus.CANCELLED),
            str(ExecutionStatus.TIMED_OUT),
            str(ExecutionPhase.REJECTED),
            str(ExecutionPhase.CANCEL_REQUESTED),
            str(ExecutionPhase.RECOVERING),
            # reconciliation aggregates a mixed-outcome parent
            "PARTIAL_COMPLETED",
        }
    ),
    str(ExecutionStatus.RUNNING): frozenset(
        {
            str(ExecutionStatus.COMPLETED),
            str(ExecutionStatus.FAILED),
            str(ExecutionStatus.BLOCKED),
            str(ExecutionStatus.CANCELLED),
            str(ExecutionStatus.TIMED_OUT),
            str(ExecutionStatus.STALLED),
            str(ExecutionPhase.CANCEL_REQUESTED),
            str(ExecutionPhase.RECOVERING),
            str(ExecutionPhase.REJECTED),
            # forced teardown of a live record
            str(ExecutionPhase.TERMINATED),
            "PARTIAL_COMPLETED",
        }
    ),
    # A suspended record is still live work: it may be resumed and finish, so
    # COMPLETED is reachable directly from here, not only via RUNNING.
    "SUSPENDED": frozenset(
        {
            str(ExecutionStatus.RUNNING),
            str(ExecutionStatus.COMPLETED),
            str(ExecutionStatus.BLOCKED),
            str(ExecutionStatus.FAILED),
            str(ExecutionStatus.CANCELLED),
            str(ExecutionStatus.TIMED_OUT),
            str(ExecutionPhase.RECOVERING),
            str(ExecutionPhase.REJECTED),
        }
    ),
    str(ExecutionStatus.STALLED): frozenset(
        {
            str(ExecutionStatus.RUNNING),
            str(ExecutionStatus.FAILED),
            str(ExecutionStatus.CANCELLED),
            str(ExecutionStatus.TIMED_OUT),
        }
    ),
    str(ExecutionPhase.CANCEL_REQUESTED): frozenset(
        {
            str(ExecutionStatus.CANCELLED),
            str(ExecutionStatus.FAILED),
            str(ExecutionStatus.TIMED_OUT),
        }
    ),
    str(ExecutionPhase.RECOVERING): frozenset(
        {
            str(ExecutionStatus.RUNNING),
            str(ExecutionStatus.COMPLETED),
            str(ExecutionStatus.FAILED),
            str(ExecutionStatus.CANCELLED),
            str(ExecutionStatus.TIMED_OUT),
            str(ExecutionPhase.RECOVERED),
        }
    ),
    str(ExecutionStatus.FAILED): frozenset({str(ExecutionPhase.RECOVERING)}),
    # A resting BLOCKED projection may converge to FAILED, but only through
    # the TTL sweeper (sweep_blocked_records): the blocker is preserved as
    # the failure detail, so the reason is never lost. No other writer may
    # take this edge, which is why the table names it explicitly instead of
    # leaving BLOCKED with no way out but RECOVERING.
    str(ExecutionStatus.BLOCKED): frozenset(
        {str(ExecutionPhase.RECOVERING), str(ExecutionStatus.FAILED)}
    ),
    str(ExecutionStatus.TIMED_OUT): frozenset({str(ExecutionPhase.RECOVERING)}),
}


class ExecutionFailureClass(StrEnum):
    """L0 runtime failure taxonomy.

    Scoped to the execution substrate on purpose. Provider-side and
    worker-side classification belongs to L1/L2, where the executor and the
    provider runtime actually live; a shell process that never started has
    nothing useful to say about quota.

    ``TOOL_TIMEOUT`` / ``PROCESS_TIMEOUT`` are deliberately distinct: the first
    is a caller-supplied budget the tool itself declared, the second is a child
    process that outlived its deadline. Collapsing them makes it impossible to
    tell "we asked for too little time" from "the process hung".
    """

    PERMISSION_DENIED = "PERMISSION_DENIED"
    TARGET_RESOLUTION_FAILURE = "TARGET_RESOLUTION_FAILURE"
    PROCESS_START_FAILURE = "PROCESS_START_FAILURE"
    PROCESS_RUNTIME_FAILURE = "PROCESS_RUNTIME_FAILURE"
    #: The command ran to completion and reported failure. Reserved for tools
    #: whose purpose is to run something and relay its verdict (test.run,
    #: build.run), where a non-zero exit is the requested answer rather than a
    #: misbehaving process. A signal death or a spawn failure is never this.
    TEST_SUITE_FAILED = "TEST_SUITE_FAILED"
    TOOL_TIMEOUT = "TOOL_TIMEOUT"
    PROCESS_TIMEOUT = "PROCESS_TIMEOUT"
    CANCELLED = "CANCELLED"
    UNKNOWN = "UNKNOWN"


class IllegalTransition(RuntimeError):
    """A writer tried to move a record somewhere it may not go.

    Declared independently of :class:`ExecutionError` so the gate can sit with
    the phase tables it validates, before the error taxonomy is defined.
    """

    code = "ILLEGAL_TRANSITION"

    def __init__(self, current: str, target: str) -> None:
        super().__init__(f"illegal execution transition {current!r} -> {target!r}")
        self.current = str(current)
        self.target = str(target)


def is_terminal_status(status: str) -> bool:
    return str(status) in TERMINAL_STATUSES


def assert_legal_transition(current: str, target: str) -> None:
    """Refuse an illegal state change instead of silently applying it.

    Terminal records are immutable, with one exception: a failed, blocked or
    timed-out record may enter RECOVERING, because that is the recovery path
    re-admitting real work. Nothing else may leave a terminal state.
    """

    cur, tgt = str(current), str(target)
    if cur == tgt:
        return
    allowed = _LEGAL_TRANSITIONS.get(cur)
    if allowed is None or tgt not in allowed:
        raise IllegalTransition(cur, tgt)


class ExecutionType(StrEnum):
    """Which canonical execution family a durable record belongs to.

    ``HICODE`` is a retained historical label only. The Hicode executor was
    retired: nothing dispatches to it, and it is never a default.
    """

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

#: How long a ``BLOCKED`` projection may rest before it is converged to a
#: terminal failure. ``BLOCKED`` is a terminal *phase* (the worker stopped), but
#: on its own it is not a terminal *status*: a blocked dispatch that is never
#: retried or cancelled would otherwise stay visible as BLOCKED forever. The TTL
#: keeps an in-flight block observable long enough to be inspected or resumed.
_BLOCKED_TERMINAL_TTL_S = float(os.environ.get("VEYA_BLOCKED_TERMINAL_TTL_S", "900") or 900)
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
    ExecutionStatus.TIMED_OUT: "TIMED_OUT",
}


class ExecutionError(Exception):
    """Stable, serializable execution error (``code`` is a remote error code)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = str(code)
        self.message = message


class ExecutionRejected(ExecutionError):
    """Admission refused the request before any worker started.

    Distinct from a runtime block: nothing ran, so there is no execution to
    report a failure from. It lands the record in REJECTED rather than
    BLOCKED so an operator can tell "we never tried" from "we tried and it
    stopped".
    """


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
    #: Resolved execution target (CANONICAL_WORKTREE | NEW_ISOLATED_WORKTREE |
    #: EXECUTION_WORKTREE | EXISTING_WORKTREE | HOST). The caller must be able
    #: to see which checkout actually ran, because the requested workspace and
    #: the resolved checkout are not the same thing.
    target_type: str | None = None
    #: Whether the resolved checkout carried local state (tracked edits, staged
    #: files, untracked source) at dispatch time. A canonical run on a dirty tree
    #: and a clean worktree are not interchangeable, and the receipt has to say
    #: which one happened.
    dirty_state: bool | None = None
    isolated_worktree: bool = False
    keep_worktree: bool = False
    # Set when the terminal path successfully reclaimed this execution's
    # worktree.  Durable evidence that the lease was released, so a restarted
    # gateway can tell "already released" from "never released".
    worktree_released_at: float | None = None
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
    execution_policy: dict[str, int] = field(default_factory=dict)
    #: Which clock expired (SUBMIT | PROCESS | TOOL | EXECUTION) and the budget it
    #: ran against. A single "timed out" answer cannot distinguish a caller that
    #: under-specified its budget from a child process that hung.
    #: Admission outcome, recorded independently of the lifecycle. A terminal
    #: record is not thereby admitted: a refusal is terminal *because* it never
    #: started, which the lifecycle alone cannot express.
    admission_status: str = "ACCEPTED"
    admission_reason: str = "NONE"
    admission_failure_class: str | None = None
    timeout_type: str | None = None
    timeout_seconds: float | None = None
    #: Structured form of the same timeout (P0.1): clock, layer, budget source,
    #: whether the budget was declared, and elapsed time. ``timeout_type`` /
    #: ``timeout_seconds`` above stay populated — this is the projection, not a
    #: replacement, so no existing reader of those fields breaks.
    timeout_attribution: dict[str, Any] | None = None
    #: When the execution last showed forward progress, so an inactivity
    #: timeout can be told apart from a total-runtime timeout.
    last_progress_at: float | None = None
    heartbeat_timeout_sec: float | None = None
    # P0-D/E/F: direct execution identity and bounded streaming state.
    execution_type: str = str(ExecutionType.DIRECT)
    command: str | None = None
    cwd: str | None = None
    profile: str | None = None
    stdout_tail: str = ""
    stderr_tail: str = ""
    bytes_stdout: int = 0
    bytes_stderr: int = 0
    # Whether the stored tail is the whole stream or a cut of it. ``direct_exec``
    # computes this while pumping the pipes, but nothing carried it into the
    # record, so ``to_public`` presented a bounded tail beside the true byte
    # count with no way to tell the two apart: a caller reading a truncated
    # 4000-character tail had no signal that it was truncated.
    stdout_truncated: bool = False
    stderr_truncated: bool = False
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
    resumed_from_execution_id: str | None = None
    checkpoint_id: str | None = None
    resume_idempotency_key: str | None = None
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
    provider_request: ProviderRequest | None = None
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
    # Split attribution (spec P5.3). Exactly one of these is set when a
    # failure is recorded; `failure_class` stays as the single log code.
    executor_failure_class: str | None = None
    provider_failure_class: str | None = None
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
    # P0-P: the direct path's Verifier outcome, projected for audit. These are
    # OBSERVATION only. They do not decide ExecutionRecord status (that is
    # _finish) and they do not decide GoalRun status (that is
    # _decide_goal_completion). P0-O-G3 records that those are still two
    # separate terminal authorities; keeping them apart here is deliberate so
    # this phase does not quietly merge them.
    verification_result: str | None = None
    verification_summary: str | None = None
    # SF-RECEIPT: the receipt contract outcome, kept as three distinct states so
    # "no receipt" can never be confused with "a receipt that was not one".
    #   valid   - a well-formed receipt is stored in effect_receipt
    #   absent  - no receipt was supplied, and that is legitimate
    #   invalid - something was supplied that is not a receipt; see the error
    receipt_contract_status: str | None = None
    receipt_contract_error: str | None = None
    last_event_cursor: int = 0
    idempotency_key: str | None = None
    task_contract: dict[str, Any] | None = None
    effect_receipt: dict[str, Any] | None = None
    finalization_status: str | None = None
    finalization_failure_class: str | None = None
    # ``dispatch_id`` is the MCP admission idempotency key.  It is distinct
    # from execution_id so a caller can safely retry admission after a lost
    # response and still recover the same durable execution.
    dispatch_id: str | None = None
    # Control-plane state is deliberately separate from the legacy provider
    # phase.  Every admission boundary is persisted before the next one.
    lifecycle_state: str = "REQUESTED"
    cancellation_intent: dict[str, Any] | None = None
    parent_reconciled_at: float | None = None

    @property
    def is_direct(self) -> bool:
        return self.execution_type == str(ExecutionType.DIRECT)

    @property
    def is_terminal(self) -> bool:
        return self.phase in {str(p) for p in TERMINAL_PHASES}

    @property
    def timeout_policy(self) -> ExecutionTimeoutPolicy:
        """The clocks this execution declared, read back off the record.

        Reading it cannot widen it: the policy is a projection of three existing
        fields, so a caller that wants a different budget must change the record,
        not the projection.
        """

        return ExecutionTimeoutPolicy.from_record(self)

    @property
    def elapsed_s(self) -> float | None:
        """How long the execution has been running, or ``None`` if it never started.

        This is what an execution-deadline check measures against. It is
        deliberately based on ``started_at`` rather than on construction time so
        an admission delay is not charged to the execution budget.
        """

        if self.started_at is None:
            return None
        end = self.completed_at if self.completed_at is not None else time.time()
        return max(0.0, end - self.started_at)

    def deadline_expired(self, now: float | None = None) -> bool:
        """Whether the execution's own total-runtime deadline has passed.

        Independent of the lifecycle tables on purpose: an execution can sit well
        past its deadline while ``phase`` is still ``RUNNING`` and no child clock
        has fired. That gap is why an execution deadline could previously only
        be reported as a tool or process timeout.
        """

        elapsed = self.elapsed_s
        if elapsed is None:
            return False
        return self.timeout_policy.expired(TimeoutKind.EXECUTION, elapsed)

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
            "dispatch_id": self.dispatch_id,
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
            # Which execution target actually ran, and the state of that
            # checkout. A caller cannot infer this from requested_workspace:
            # the same request resolves to different checkouts by intent.
            "target_type": self.target_type,
            "dirty_state": self.dirty_state,
            "admission_status": self.admission_status,
            "admission_reason": self.admission_reason,
            "admission_failure_class": self.admission_failure_class,
            "timeout_type": self.timeout_type,
            "timeout_seconds": self.timeout_seconds,
            "timeout_attribution": self.timeout_attribution,
            "last_progress_at": self.last_progress_at,
            "worktree_binding_key": self.worktree_binding_key,
            "worktree_base_sha": self.worktree_base_sha,
            "execution_commit_sha": self.execution_commit_sha,
            "promotion_state": self.promotion_state,
            "canonical_after_sha": self.canonical_after_sha,
            "task_contract": dict(self.task_contract or {}),
            "effect_receipt": dict(self.effect_receipt or {}),
            # SF-RECEIPT: a caller can see which of the three receipt states this
            # record is in, instead of inferring it from a missing receipt.
            "receipt_contract_status": self.receipt_contract_status,
            "receipt_contract_error": self.receipt_contract_error,
            # P0-P: the direct path's Verifier outcome. Projected because a
            # receipt that cannot show whether it was verified is not a truthful
            # receipt. Observation only; it decides neither terminal status.
            "verification_result": self.verification_result,
            "verification_summary": self.verification_summary,
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
            "provider_request": None if self.provider_request is None else {
                "request_id": self.provider_request.request_id,
                "execution_id": self.provider_request.execution_id,
                "goal_run_id": self.provider_request.goal_run_id,
                "provider": self.provider_request.provider,
                "model": self.provider_request.model,
                "status": str(self.provider_request.status),
                "timeout_ms": self.provider_request.timeout_ms,
                "started_at": self.provider_request.started_at,
                "completed_at": self.provider_request.completed_at,
                "error_code": self.provider_request.error_code,
            },
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
            "executor_failure_class": self.executor_failure_class,
            "provider_failure_class": self.provider_failure_class,
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
            "execution_policy": dict(self.execution_policy),
            "heartbeat_timeout_sec": self.heartbeat_timeout_sec,
            # Legacy aliases kept for existing callers/tests.
            "state": (
                _LEGACY_STATE.get(ExecutionStatus(status), status)
                if status in ExecutionStatus._value2member_map_
                else status
            ),
            "workspace": self.requested_realpath,
            "session_id": self.session_id,
            "lifecycle_state": self.lifecycle_state,
            "cancellation_intent": dict(self.cancellation_intent or {}),
            "parent_reconciled_at": self.parent_reconciled_at,
        }
        if self.is_direct:
            payload.update(
                {
                    "command": self.command,
                    "cwd": self.cwd,
                    "profile": self.profile,
                    "stdout_tail": self.stdout_tail,
                    "stderr_tail": self.stderr_tail,
                    # Whether the tails above are the whole stream or a cut of
                    # it. Without these a caller cannot tell a short output from
                    # a clipped one, and would quote a truncated tail as if it
                    # were everything the process said.
                    "stdout_truncated": self.stdout_truncated,
                    "stderr_truncated": self.stderr_truncated,
                    "is_terminal": self.is_terminal,
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
        provider_request = clean.get("provider_request")
        if isinstance(provider_request, dict):
            provider_request = ProviderRequest(
                execution_id=str(provider_request.get("execution_id") or clean.get("execution_id") or ""),
                goal_run_id=provider_request.get("goal_run_id"),
                provider=provider_request.get("provider"),
                model=provider_request.get("model"),
                request_id=str(provider_request.get("request_id") or ""),
                status=ProviderRequestStatus(str(provider_request.get("status") or "REQUESTED")),
                timeout_ms=provider_request.get("timeout_ms"),
                started_at=provider_request.get("started_at"),
                completed_at=provider_request.get("completed_at"),
                error_code=provider_request.get("error_code"),
            )
            clean["provider_request"] = provider_request
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

    def set_target(self, target_type: str, dirty_state: bool | None = None) -> None:
        self._manager.set_target(
            self._execution_id, target_type=target_type, dirty_state=dirty_state
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

    def admission(self, decision: Any) -> None:
        """Record the admission decision for this execution."""

        self._manager.set_admission(self._execution_id, decision)

    def timeout(self, *, kind: str, seconds: float | None = None) -> None:
        """Record which clock expired, so a timeout is never undifferentiated."""

        self._manager.set_timeout(self._execution_id, kind=kind, seconds=seconds)

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
        stdout_truncated: bool | None = None,
        stderr_truncated: bool | None = None,
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
            stdout_truncated=stdout_truncated,
            stderr_truncated=stderr_truncated,
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
        checkpoint_root = self.store.root if self.store.root is not None else Path.home() / ".veya" / "remote_executions"
        self.checkpoint_store = ExecutionCheckpointStore(checkpoint_root)
        self.recovery_failures: list[dict[str, Any]] = []
        # Durable-write failures must be observable; a swallowed projection
        # write can silently lose a terminal/recovery transition.
        self.persistence_failures: list[dict[str, Any]] = []
        self._records: dict[str, ExecutionRecord] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._monitors: dict[str, asyncio.Task[None]] = {}
        # D5 observability: leak-relevant resource counters.  These exist so
        # "no leak" is a measured claim rather than an inference.
        self._metrics: dict[str, int] = {}
        self._lock = threading.RLock()
        self._load()

    def metrics_bump(self, name: str, amount: int = 1) -> None:
        with self._lock:
            self._metrics[name] = self._metrics.get(name, 0) + int(amount)

    def metrics_snapshot(self) -> dict[str, int]:
        """Point-in-time copy of the resource counters (D5 gate input)."""
        with self._lock:
            return dict(self._metrics)

    # ── persistence ─────────────────────────────────────────────────
    def _load(self) -> None:
        for record in self.store.load_all():
            self._records[record.execution_id] = record
        self._reconcile_stale_parents()

    def _reconcile_stale_parents(self) -> int:
        """Fail closed stale parent projections left by timed-out dispatch RPCs.

        A parent worker.dispatch execution that never produced children and
        whose heartbeat is dead is a frozen projection from a timed-out RPC,
        not live work.  Reconcile it to FAILED so it stops consuming durable
        capacity and blocking recovery.
        """
        reconciled = 0
        now = time.time()
        for record in list(self._records.values()):
            if record.is_terminal or record.role != "parent":
                continue
            if record.child_execution_ids:
                continue
            if record.tool != "worker.dispatch":
                continue
            heartbeat_at = record.heartbeat_at
            if heartbeat_at and now - heartbeat_at < 300:
                continue
            self._finish(
                record,
                str(ExecutionStatus.FAILED),
                message="stale dispatch projection reconciled at startup",
                error="STALE_DISPATCH_PROJECTION",
            )
            reconciled += 1
        return reconciled

    @staticmethod
    def _is_reclaimable_worktree(path: str) -> bool:
        """Only Local2-created *execution* worktrees are ever reclaimable.

        ``teardown_worktree`` guards on the ``.veya/worktrees`` path prefix, but
        that directory also holds hand-made feature worktrees
        (``agy-remove-dangerous-bypass``, ``v2-baseline-lint``, ...).  Those are
        user state, not execution garbage.  WorktreeManager creates execution
        worktrees as ``<base_dir>/task-<task_id>``, so require that exact shape
        before reclaiming anything.
        """
        try:
            resolved = Path(path).expanduser().resolve()
        except (OSError, RuntimeError):
            return False
        if resolved.name == "task-" or not resolved.name.startswith("task-"):
            return False
        return any(part == ".veya" for part in resolved.parts) and any(
            part == "worktrees" for part in resolved.parts
        )

    def reconcile_worktrees(self, *, dry_run: bool = False) -> dict[str, Any]:
        """Reclaim execution worktrees whose execution is durably terminal.

        This is the crash/restart-safe half of the worktree lifecycle.  The
        normal terminal path releases inline, but a gateway that died between
        "terminal persisted" and "worktree removed" leaves the worktree behind
        forever.  Startup and the operator GC both call this.

        Safety properties:
        - only records that are durably terminal are considered
        - ``keep_worktree`` records are never touched
        - a non-terminal record's worktree is never touched, even if the
          process died (an in-flight execution may still be writing)
        - teardown delegates to ``teardown_worktree``, which is fail-closed on
          dirty / locked / /proc-referenced worktrees and only ever resolves
          paths under ``.veya/worktrees``
        - idempotent: a released worktree reports NOT_FOUND and is skipped
        """
        from runtime.coding.worktree import teardown_worktree

        released: list[str] = []
        retained: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        considered = 0
        now = time.time()
        with self._lock:
            records = list(self._records.values())
        for record in records:
            path = record.worktree_path
            if not path:
                continue
            considered += 1
            if not record.is_terminal:
                retained.append({"execution_id": record.execution_id, "reason": "NOT_TERMINAL"})
                continue
            if record.keep_worktree:
                retained.append({"execution_id": record.execution_id, "reason": "KEEP_WORKTREE"})
                continue
            if record.worktree_released_at is not None and not Path(path).exists():
                continue  # already released in a previous run
            if not Path(path).exists():
                if record.worktree_released_at is None:
                    record.worktree_released_at = now
                continue
            if not self._is_reclaimable_worktree(path):
                retained.append(
                    {
                        "execution_id": record.execution_id,
                        "path": path,
                        "reason": "NOT_EXECUTION_WORKTREE",
                    }
                )
                continue
            if dry_run:
                retained.append(
                    {"execution_id": record.execution_id, "reason": "DRY_RUN", "path": path}
                )
                continue
            try:
                outcome = teardown_worktree(path, execution_status=str(record.status))
            except Exception as exc:  # teardown is best-effort per worktree
                errors.append(
                    {
                        "execution_id": record.execution_id,
                        "path": path,
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )
                continue
            if outcome.get("cleaned"):
                released.append(record.execution_id)
                record.worktree_released_at = time.time()
                self.metrics_bump("worktrees_released", 1)
                with contextlib.suppress(Exception):
                    self._persist(record)
            else:
                retained.append(
                    {
                        "execution_id": record.execution_id,
                        "path": path,
                        "reason": str(outcome.get("status")),
                    }
                )
                self.metrics_bump("worktrees_retained", 1)
        return {
            "dry_run": dry_run,
            "considered": considered,
            "released": len(released),
            "released_execution_ids": released,
            "retained": retained,
            "errors": errors,
        }

    def unfinished_count(self) -> int:
        """Number of persisted non-terminal remote projections."""
        with self._lock:
            return sum(not record.is_terminal for record in self._records.values())

    def unfinished_records(self) -> list[ExecutionRecord]:
        """Snapshot of non-terminal projections for lifecycle decisions."""
        with self._lock:
            return [record for record in self._records.values() if not record.is_terminal]

    def blocked_record_count(self) -> int:
        """How many projections are currently resting in ``BLOCKED``."""

        with self._lock:
            return sum(
                1
                for record in self._records.values()
                if record.status == str(ExecutionStatus.BLOCKED)
                and record.phase == str(ExecutionPhase.BLOCKED)
            )

    def sweep_blocked_records(self, *, ttl_s: float | None = None) -> int:
        """Converge long-resting ``BLOCKED`` projections to ``FAILED``.

        ``BLOCKED`` records are terminal in phase but were never given a
        terminal status, so nothing else advances them. After ``ttl_s`` they are
        finished as FAILED, preserving the original blocker as the failure
        detail so the reason is never lost. Returns the number converged.
        """

        cutoff = time.time() - (_BLOCKED_TERMINAL_TTL_S if ttl_s is None else float(ttl_s))
        converged = 0
        with self._lock:
            candidates = [
                record
                for record in self._records.values()
                if record.status == str(ExecutionStatus.BLOCKED)
                and record.phase == str(ExecutionPhase.BLOCKED)
                and (record.completed_at or record.started_at or 0.0) <= cutoff
            ]
        for record in candidates:
            self._finish(
                record,
                str(ExecutionStatus.FAILED),
                message="blocked execution exceeded its terminal TTL",
                error=record.failure_class or record.error or "BLOCKED_TTL_EXPIRED",
            )
            converged += 1
        return converged

    def reconcile_unfinished(self) -> dict[str, int]:
        """Converge stale child projections and mechanical parents.

        A stale heartbeat alone is not terminal evidence.  Reconciliation
        requires the durable worker lease to be absent *and* persisted failure,
        timeout, cancellation, or direct-command evidence.  Parents are then
        aggregated only after their children have been classified.
        """

        children_reconciled = 0
        parents_reconciled = 0
        blocked_swept = self.sweep_blocked_records()
        with self._lock:
            records = list(self._records.values())
            tasks = dict(self._tasks)
        for record in records:
            if record.is_terminal or record.role == "parent":
                continue
            task = tasks.get(record.execution_id)
            task_live = task is not None and not task.done()
            orphaned_worker = self._orphaned_worker(record, task_live=task_live)
            failure_evidence = bool(
                record.failure_class
                or record.error
                or record.direct_status in {"failed", "timeout", "denied", "approval_required"}
            )
            if record.cancel_requested and not task_live:
                self._finish(record, str(ExecutionStatus.CANCELLED), message="execution cancelled")
                children_reconciled += 1
            elif not task_live and (failure_evidence or orphaned_worker):
                self._finish(
                    record,
                    str(ExecutionStatus.FAILED),
                    message=record.failure_detail or record.error or "stale worker failed",
                    error=record.failure_class or record.error or "STALE_WORKER_FAILURE",
                )
                children_reconciled += 1
        children_by_parent: dict[str, list[ExecutionRecord]] = {}
        for child in records:
            if child.parent_execution_id:
                children_by_parent.setdefault(child.parent_execution_id, []).append(child)
        for parent in records:
            if parent.role != "parent" or parent.is_terminal:
                continue
            before = (str(parent.status), str(parent.phase))
            children = children_by_parent.get(parent.execution_id, [])
            known = {child.execution_id for child in children}
            children.extend(
                child
                for child in records
                if child.execution_id in parent.child_execution_ids
                and child.execution_id not in known
            )
            self.aggregate(parent, _children=children)
            if before != (str(parent.status), str(parent.phase)) and parent.is_terminal:
                parents_reconciled += 1
        return {
            "children": children_reconciled,
            "parents": parents_reconciled,
            "blocked_swept": blocked_swept,
        }

    def reconcile_prelaunch(self) -> int:
        """Fail closed pre-admissions that have no child launch evidence."""
        reconciled = 0
        for record in self.unfinished_records():
            if (
                record.role != "parent"
                or record.child_execution_ids
                or not record.dispatch_id
                or not record.goal_run_id
            ):
                continue
            if record.lifecycle_state not in {
                "REQUESTED",
                "VALIDATED",
                "ADMITTED",
                "PERSISTED",
                "DISPATCHED",
            }:
                continue
            from server.goal_run.pre_admission import fail_pre_admission

            reason = "backend restarted before worker launch"
            if record.goal_project_root and record.goal_run_id:
                fail_pre_admission(
                    project_root=record.goal_project_root,
                    goal_run_id=record.goal_run_id,
                    reason=reason,
                )
            self._finish(
                record, str(ExecutionStatus.FAILED), message=reason, error="PRELAUNCH_RECOVERY"
            )
            reconciled += 1
        return reconciled

    def _orphaned_worker(self, record: ExecutionRecord, *, task_live: bool) -> bool:
        """Return true only for durable evidence of a dead admitted worker.

        A stale heartbeat by itself remains a visible ``STALLED`` projection.
        Terminal reconciliation additionally requires an admitted process
        identity, a missing in-process task, an expired heartbeat, and proof
        that the recorded worker process is no longer alive.  This prevents a
        quiet but healthy provider request from being converted into failure,
        while allowing restart recovery to converge orphaned children.
        """
        if task_live or record.is_terminal or record.worker_pid is None:
            return False
        if record.phase in {str(ExecutionPhase.QUEUED), str(ExecutionPhase.STARTING)}:
            return False
        if record.heartbeat_at is None:
            return False
        if time.time() - record.heartbeat_at <= self.heartbeat_timeout_s:
            return False
        try:
            os.kill(record.worker_pid, 0)
        except (ProcessLookupError, PermissionError):
            return True
        except OSError:
            return True
        return False

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
        self.reconcile_unfinished()
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
        checkpoint_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> ExecutionRecord:
        """Resume a durable checkpoint as a new Execution on the same GoalRun."""
        record = self.status(
            execution_id,
            token_id=token_id,
            workspace_realpath=workspace_realpath,
            principal=principal,
        )
        if record.goal_run_id is None:
            raise ExecutionError("GOAL_RUN_MISSING", "remote execution has no canonical GoalRun")

        key = idempotency_key or checkpoint_id or execution_id
        with self._lock:
            for existing in self._records.values():
                if (
                    existing.resumed_from_execution_id == execution_id
                    and existing.resume_idempotency_key == key
                ):
                    return existing

        try:
            checkpoint = self.checkpoint_store.read_durable(checkpoint_id)
        except CheckpointError as exc:
            raise ExecutionError("CHECKPOINT_INVALID", str(exc)) from exc
        if checkpoint is None:
            raise ExecutionError("CHECKPOINT_MISSING", "no durable execution checkpoint is available")
        if checkpoint.goal_run_id != record.goal_run_id or checkpoint.execution_id != execution_id:
            raise ExecutionError(
                "CHECKPOINT_LINEAGE_MISMATCH",
                "checkpoint does not belong to the requested execution lineage",
            )
        if record.is_terminal and record.status not in {
            str(ExecutionStatus.FAILED),
            str(ExecutionStatus.TIMED_OUT),
            str(ExecutionStatus.BLOCKED),
        }:
            raise ExecutionError("EXECUTION_NOT_RESUMABLE", f"execution is terminal: {record.status}")

        chosen = runner or (
            self.recovery_runner_factory(record) if self.recovery_runner_factory else None
        )
        if chosen is None:
            raise ExecutionError(
                "RECOVERY_RUNNER_MISSING", "provider recovery factory is unavailable"
            )

        from types import SimpleNamespace

        binding = SimpleNamespace(
            requested_path=record.requested_workspace,
            requested_realpath=record.requested_realpath,
            repo_root=record.resolved_repo_root,
            repo_identity=record.repo_identity,
            worktree_path=record.worktree_path,
            worktree_repo_root=record.worktree_repo_root,
            base_sha=record.worktree_base_sha,
        )
        session = SimpleNamespace(
            session_id=record.session_id,
            token_id=record.token_id,
            principal=record.principal,
        )
        child = self.submit(
            session=session,
            tool=record.tool,
            veya_tool=record.veya_tool,
            binding=binding,
            runner=chosen,
            task_id=record.goal_task_id or record.task_id,
            limits={
                "max_steps": record.max_steps,
                "execution_timeout_sec": record.execution_timeout_sec,
                "effective_timeout_ms": record.effective_timeout_ms,
                "command_timeout_sec": record.command_timeout_sec,
            },
            execution_type=record.execution_type,
            command=record.command,
            cwd=checkpoint.working_directory,
            profile=record.profile,
            execution_mode=record.execution_mode,
            orchestrator=record.orchestrator,
            worker_type=record.worker_type,
            worker_id=record.worker_id,
            model_provider=record.model_provider,
            model=record.model,
            parent_execution_id=execution_id,
            role=record.agent_role,
            preferred_worker_runtime_id=record.preferred_worker_runtime_id,
            idempotency_key=key,
            goal_run_id=record.goal_run_id,
            goal_task_id=record.goal_task_id or record.task_id,
            goal_project_root=record.goal_project_root,
            spec=record.spec,
            task_contract=record.task_contract,
            keep_worktree=True,
        )
        child.resumed_from_execution_id = execution_id
        child.checkpoint_id = checkpoint.checkpoint_id
        child.resume_idempotency_key = key
        child.phase = ExecutionPhase.RESUMING
        child.message = "resuming from durable checkpoint"
        child.events.append(
            {
                "ts": time.time(),
                "kind": "resumed",
                "phase": str(ExecutionPhase.RESUMING),
                "checkpoint_id": checkpoint.checkpoint_id,
                "resumed_from_execution_id": execution_id,
            }
        )
        self._persist(child, required=True)
        return child

    def _persist(self, record: ExecutionRecord, *, required: bool = False) -> None:
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
            if required:
                raise ExecutionError(
                    "PERSISTENCE_FAILED",
                    f"durable persistence failed for {record.execution_id}",
                ) from exc

    def transition_lifecycle(
        self, execution_id: str, state: str, *, required: bool = True
    ) -> ExecutionRecord:
        """Persist one canonical control-plane boundary before returning it."""
        allowed = {
            "REQUESTED",
            "VALIDATED",
            "ADMITTED",
            "PERSISTED",
            "DISPATCHED",
            "RUNNING",
            "COMPLETED",
            "FAILED",
            "TIMED_OUT",
            "CANCELLED",
            "CANCEL_REQUESTED",
            "REJECTED",
            "RECOVERING",
            "RECOVERED",
        }
        normalized = str(state).upper()
        if normalized not in allowed:
            raise ExecutionError("INVALID_STATE", f"unsupported lifecycle state {state!r}")
        record = self._record_for_update(execution_id)
        if record.is_terminal and normalized != record.lifecycle_state:
            # Refused, but not silently: a caller that believes it advanced a
            # finished execution must be able to see that it did not.
            record.events.append(
                {
                    "ts": time.time(),
                    "kind": "ILLEGAL_TRANSITION",
                    "phase": str(record.lifecycle_state),
                    "message": (
                        f"lifecycle {record.lifecycle_state} -> {normalized} refused: "
                        "terminal execution is immutable"
                    ),
                }
            )
            self._persist(record)
            return record
        record.lifecycle_state = normalized
        if normalized == "RUNNING":
            record.status = str(ExecutionStatus.RUNNING)
        elif normalized in {"COMPLETED", "FAILED", "TIMED_OUT", "CANCELLED"}:
            record.status = normalized
            record.phase = record.status
        self._persist(record, required=required)
        return record

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
        execution_type: str = str(ExecutionType.DIRECT),
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
        dispatch_id: str | None = None,
        goal_run_id: str | None = None,
        goal_task_id: str | None = None,
        goal_project_root: str | None = None,
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
        if isolated_worktree and not worktree_path:
            repo_root = getattr(binding, "repo_root", None) or getattr(
                binding, "requested_realpath", None
            )
            if repo_root and Path(repo_root).exists():
                try:
                    from runtime.coding.worktree import WorktreeManager

                    mgr = WorktreeManager(repo_root)
                    record_wt = mgr.create(
                        task_id=task_id or execution_id, objective=tool or "task"
                    )
                    worktree_path = str(record_wt.path)
                    worktree_repo_root = str(record_wt.repo_root)
                    worktree_branch = record_wt.branch_name
                except Exception:
                    pass

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
            dispatch_id=dispatch_id,
            lifecycle_state="ADMITTED",
            goal_run_id=goal_run_id,
            goal_task_id=goal_task_id,
            goal_project_root=goal_project_root,
        )
        if record.dispatch_id and not record.goal_run_id:
            raise ExecutionError(
                "GOAL_RUN_MISSING",
                "dispatch execution requires a canonical pre-admitted GoalRun",
            )
        for field_name in (
            "max_steps",
            "execution_timeout_sec",
            "effective_timeout_ms",
            "command_timeout_sec",
        ):
            if limits and limits.get(field_name) is not None:
                setattr(record, field_name, limits[field_name])
        requested_timeout_s = (
            limits.get("execution_timeout_sec")
            if limits
            else None
        ) or (
            limits.get("command_timeout_sec")
            if limits
            else None
        ) or 1800.0
        record.execution_policy = ExecutionPolicy.from_legacy(
            float(requested_timeout_s),
            separated_cli=True,
        ).to_dict()
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
            self._persist(record, required=True)
            record.lifecycle_state = "PERSISTED"
            self._persist(record, required=True)
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
        dispatch_id: str | None = None,
        executor_id: str | None = None,
        goal_run_id: str | None = None,
        goal_task_id: str | None = None,
        goal_project_root: str | None = None,
        task_contract: dict[str, Any] | None = None,
    ) -> ExecutionRecord:
        """Create a parent aggregator execution with no worker of its own."""
        with self._lock:
            if dispatch_id:
                for existing in self._records.values():
                    if existing.dispatch_id == dispatch_id:
                        return existing
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
            worker_id=executor_id or f"parent_{uuid.uuid4().hex[:12]}",
            phase="QUEUED",
            role="parent",
            failure_mode=failure_mode,
            parent_execution_id=parent_execution_id,
            heartbeat_at=time.time(),
            dispatch_id=dispatch_id,
            idempotency_key=dispatch_id,
            lifecycle_state="REQUESTED",
            goal_run_id=goal_run_id,
            goal_task_id=goal_task_id,
            goal_project_root=goal_project_root,
            task_contract=dict(task_contract or {}),
        )
        if dispatch_id and not record.goal_run_id:
            raise ExecutionError(
                "GOAL_RUN_MISSING",
                "dispatch parent requires a canonical pre-admitted GoalRun",
            )
        record.events.append(
            {"ts": time.time(), "kind": "submitted", "phase": "QUEUED", "message": "parent"}
        )
        with self._lock:
            self._records[record.execution_id] = record
            self._persist(record, required=True)
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

    def aggregate(
        self,
        parent: ExecutionRecord,
        *,
        _children: list[ExecutionRecord] | None = None,
    ) -> dict[str, Any]:
        """Mechanical child aggregation. Never ranks or picks a winner."""

        # Reclassify persisted stale children before counting them. This keeps
        # status polling and startup reconciliation on the same state machine.
        children = (
            list(_children) if _children is not None else self.children_of(parent.execution_id)
        )
        for child in children:
            if child.is_terminal:
                continue
            with self._lock:
                task = self._tasks.get(child.execution_id)
            orphaned_worker = self._orphaned_worker(
                child, task_live=task is not None and not task.done()
            )
            evidence = bool(
                child.failure_class
                or child.error
                or child.direct_status in {"failed", "timeout", "denied", "approval_required"}
            )
            if (task is None or task.done()) and (evidence or orphaned_worker):
                self._finish(
                    child,
                    str(ExecutionStatus.FAILED),
                    message=child.failure_detail or child.error or "stale worker failed",
                    error=child.failure_class or child.error or "STALE_WORKER_FAILURE",
                )
        if _children is None:
            children = self.children_of(parent.execution_id)
        children.sort(key=lambda r: r.created_at)
        counts = {
            "total": len(children),
            "queued": 0,
            "running": 0,
            "completed": 0,
            "failed": 0,
            "blocked": 0,
            "rejected": 0,
            "cancelled": 0,
            "timed_out": 0,
        }
        summaries = []
        for child in children:
            public = child.to_public(heartbeat_timeout_s=self.heartbeat_timeout_s)
            status = public["status"]
            # Admission is read before the lifecycle. A refusal never started,
            # so reporting it as failed or blocked would attribute a runtime
            # fault to a decision the executor plane made before launching
            # anything.
            if public.get("admission_status") == "REJECTED":
                counts["rejected"] += 1
            elif status == "QUEUED":
                counts["queued"] += 1
            elif status in {"RUNNING", "STALLED"}:
                counts["running"] += 1
            elif status == "COMPLETED":
                counts["completed"] += 1
            elif status == "BLOCKED":
                counts["blocked"] += 1
            elif status == "CANCELLED":
                counts["cancelled"] += 1
            elif status == "TIMED_OUT":
                counts["timed_out"] += 1
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
        elif (
            parent.failure_mode == "fail_fast"
            and counts["failed"] + counts["blocked"] + counts["rejected"] > 0
        ):
            # A terminal child failure is sufficient evidence for fail-fast
            # aggregation. Stop any still-admitted child projection so the
            # parent cannot claim success while work continues.
            for child in children:
                if child.is_terminal:
                    continue
                child.cancel_requested = True
                with self._lock:
                    task = self._tasks.get(child.execution_id)
                if task is not None and not task.done():
                    task.cancel()
                self._finish(child, str(ExecutionStatus.CANCELLED), message="parent fail-fast")
            status, phase = "FAILED", "FAILED"
        elif counts["running"] + counts["queued"] > 0:
            status, phase = "RUNNING", "WAITING_WORKERS"
        elif counts["completed"] == counts["total"]:
            status, phase = "COMPLETED", "COMPLETED"
        elif counts["completed"] > 0:
            status, phase = "PARTIAL_COMPLETED", "COMPLETED"
        elif counts["cancelled"] == counts["total"]:
            status, phase = "CANCELLED", "CANCELLED"
        elif counts["timed_out"] == counts["total"]:
            status, phase = "TIMED_OUT", "TIMED_OUT"
        elif counts["failed"] + counts["blocked"] + counts["rejected"] == counts["total"]:
            status, phase = "FAILED", "FAILED"
        else:
            # Mixed terminal outcomes with no work left: some children failed or
            # were cancelled while others finished, and none of the uniform
            # branches above apply (e.g. collect_all with FAILED + CANCELLED).
            # The chain had no fallback, so `status`/`phase` stayed unbound and
            # `aggregate()` raised UnboundLocalError -- which runs during startup
            # reconciliation and took the whole MCP gateway down.  A parent whose
            # children did not all complete is FAILED, never a silent success.
            status, phase = "FAILED", "FAILED"
        state_changed = str(parent.status) != status or str(parent.phase) != phase
        terminal_reconciliation_missing = status in {
            "COMPLETED",
            "FAILED",
            "TIMED_OUT",
            "CANCELLED",
        } and (parent.lifecycle_state != status or parent.parent_reconciled_at is None)
        if state_changed or terminal_reconciliation_missing:
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
            elif status in {"FAILED", "BLOCKED", "TIMED_OUT"}:
                parent.phase = ExecutionPhase(status)
            else:
                parent.phase = ExecutionPhase.COMPLETED
            if (
                status in {"COMPLETED", "FAILED", "TIMED_OUT", "CANCELLED", "PARTIAL_COMPLETED"}
                and parent.completed_at is None
            ):
                parent.completed_at = time.time()
            if status in {"COMPLETED", "FAILED", "TIMED_OUT", "CANCELLED"}:
                parent.lifecycle_state = status
                parent.parent_reconciled_at = time.time()
            elif status == "RUNNING":
                parent.lifecycle_state = "RUNNING"
            qualification_checkpoint(
                "BEFORE_PARENT_RECONCILIATION",
                execution_id=parent.execution_id,
                dispatch_id=parent.dispatch_id,
                goal_run_id=parent.goal_run_id,
                goal_task_id=parent.goal_task_id,
                status=status,
            )
            self._persist(parent)
            qualification_checkpoint(
                "AFTER_PARENT_RECONCILIATION",
                execution_id=parent.execution_id,
                dispatch_id=parent.dispatch_id,
                goal_run_id=parent.goal_run_id,
                goal_task_id=parent.goal_task_id,
                status=status,
            )
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
            # Layer projection: exactly one side is named, mirroring
            # FailureAttribution.to_dict(), so a reader never has to guess
            # which layer to go and look at. Only recognised taxonomy
            # members are projected; generic codes (admission refusals,
            # timeout markers) keep both sides null rather than claiming a
            # layer they do not name.
            from veya.remote.models import ExecutorFailureClass, ProviderFailureClass

            try:
                ProviderFailureClass(str(failure_class))
                record.provider_failure_class = str(failure_class)
            except ValueError:
                try:
                    ExecutorFailureClass(str(failure_class))
                    record.executor_failure_class = str(failure_class)
                except ValueError:
                    pass
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

    async def _run_precreated_child(self, record: ExecutionRecord, runner: Runner) -> None:
        """Run one precreated task without concurrently re-entering GoalRun."""
        from server.goal_run.pre_admission import mark_task_running, reconcile_task

        reporter = ProgressReporter(self, record.execution_id)
        monitor = asyncio.create_task(
            self._monitor(record), name=f"veya-remote-heartbeat-{record.execution_id}"
        )
        self._monitors[record.execution_id] = monitor
        record.status = str(ExecutionStatus.RUNNING)
        record.lifecycle_state = "RUNNING"
        record.phase = str(ExecutionPhase.RUNNING)
        record.started_at = record.started_at or time.time()
        record.heartbeat_at = time.time()
        record.worker_alive = True
        self._persist(record, required=True)
        mark_task_running(
            project_root=record.goal_project_root or record.resolved_repo_root,
            goal_run_id=record.goal_run_id or "",
            goal_task_id=record.goal_task_id or "",
        )
        outcome = "failed"
        reason: str | None = None
        try:
            qualification_checkpoint(
                "WHILE_RUNNING",
                execution_id=record.execution_id,
                dispatch_id=record.dispatch_id,
                goal_run_id=record.goal_run_id,
                goal_task_id=record.goal_task_id,
                executor_id=record.worker_id,
            )
            with use_reporter(reporter):
                record.result_summary = str(await runner(reporter) or "")
            record.worker_final_claim = True
            outcome = "completed"
            self._finish(record, str(ExecutionStatus.COMPLETED), message="completed")
            qualification_checkpoint(
                "AFTER_WORKER_TERMINAL",
                execution_id=record.execution_id,
                dispatch_id=record.dispatch_id,
                goal_run_id=record.goal_run_id,
                goal_task_id=record.goal_task_id,
                status=record.status,
            )
        except QualificationFault as exc:
            reason = str(exc)
            if not record.is_terminal:
                self._finish(
                    record,
                    str(ExecutionStatus.FAILED),
                    message=reason,
                    error="QUALIFICATION_FAULT",
                )
        except asyncio.CancelledError:
            if record.cancel_requested:
                outcome = "cancelled"
                reason = "explicit cancellation"
                self._finish(record, str(ExecutionStatus.CANCELLED), message="execution cancelled")
            else:
                reason = "worker process cancelled"
            raise
        except ExecutionError as exc:
            reason = exc.message
            if exc.code in {"TIMEOUT", "WORKER_TIMEOUT"}:
                # A caller-supplied budget is a different event from a child
                # process that outlived its deadline. Record which clock
                # expired instead of reporting a single undifferentiated
                # "timed out".
                #
                # The clock is chosen from what the record can prove expired, and
                # the budget is then read from that same clock. Choosing by
                # convenience (``TOOL if command_timeout_sec``) and writing the
                # budget from a different field produced a receipt that named a
                # process clock while quoting a command budget, so an execution
                # whose own deadline had passed was reported as a child fault.
                policy = record.timeout_policy
                elapsed = record.elapsed_s or 0.0
                if policy.expired(TimeoutKind.EXECUTION, elapsed):
                    kind = TimeoutKind.EXECUTION
                elif record.command_timeout_sec and policy.expired(TimeoutKind.TOOL, elapsed):
                    kind = TimeoutKind.TOOL
                else:
                    kind = TimeoutKind.PROCESS
                record.failure_class = str(ExecutionFailureClass.TOOL_TIMEOUT)
                record.timeout_type = str(kind)
                record.timeout_seconds = (
                    policy.resolve(kind)
                    or record.command_timeout_sec
                    or record.execution_timeout_sec
                )
                record.timeout_attribution = policy.attribute(
                    kind,
                    elapsed_s=elapsed,
                    seconds=record.timeout_seconds,
                ).to_dict()
                self._finish(
                    record,
                    str(ExecutionStatus.TIMED_OUT),
                    message=exc.message,
                    error=str(ExecutionFailureClass.TOOL_TIMEOUT),
                )
            elif isinstance(exc, ExecutionBlocked):
                # ExecutionBlocked is raised from admission (a capability gap, a
                # workspace the gate would not bind) and from the environment
                # (worktree creation). The admission runner has already recorded
                # its decision on the record, so honour it here: a refusal is
                # reported as a refusal and never as a runtime block.
                refused = record.admission_status == str(AdmissionStatus.REJECTED)
                self.set_failure(
                    record.execution_id,
                    failure_class=(record.admission_failure_class or "execution_blocked")
                    if refused
                    else "execution_blocked",
                    source="remote_admission" if refused else "remote_execution",
                    detail=exc.message,
                    code=exc.code,
                )
                self._finish(
                    record,
                    str(ExecutionStatus.BLOCKED),
                    message=exc.message,
                    error=exc.code,
                )
            else:
                self._finish(
                    record, str(ExecutionStatus.FAILED), message=exc.message, error=exc.code
                )
        except Exception as exc:
            reason = f"{type(exc).__name__}: {exc}"
            self._finish(
                record, str(ExecutionStatus.FAILED), message=reason, error="PROVIDER_ERROR"
            )
        finally:
            if (
                record.is_terminal
                and record.goal_project_root
                and record.goal_run_id
                and record.goal_task_id
            ):
                reconcile_task(
                    project_root=record.goal_project_root,
                    goal_run_id=record.goal_run_id,
                    goal_task_id=record.goal_task_id,
                    outcome=(
                        "cancelled" if record.status == str(ExecutionStatus.CANCELLED) else outcome
                    ),
                    summary=record.result_summary or "",
                    reason=reason,
                )
            record.worker_alive = False
            self._persist(record)
            with self._lock:
                self._tasks.pop(record.execution_id, None)
                self._monitors.pop(record.execution_id, None)
            if not monitor.done():
                monitor.cancel()

    async def _admit_goal_run(self, record: ExecutionRecord, runner: Runner) -> None:
        """Admit a remote request to GoalRun and project its response.

        The nested adapter is deliberately only a provider bridge.  It has no
        scheduler, retry loop, lease, or terminal-state authority.
        """
        from pathlib import Path

        if record.dispatch_id:
            from server.goal_run.store import load_goal_run

            canonical = load_goal_run(
                record.goal_project_root or record.resolved_repo_root, record.goal_run_id or ""
            )
            if (
                canonical is None
                or canonical.dispatch_id != record.dispatch_id.split(":child:", 1)[0]
                or record.goal_task_id not in canonical.tasks
                or canonical.execution_id is None
            ):
                self._finish(
                    record,
                    str(ExecutionStatus.FAILED),
                    message="canonical GoalRun/task admission is missing",
                    error="CANONICAL_ADMISSION_MISSING",
                )
                return
            if record.parent_execution_id:
                await self._run_precreated_child(record, runner)
                return

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
                        failure_class = (
                            str(ExecutionFailureClass.TOOL_TIMEOUT)
                            if exc.code in {"TIMEOUT", "WORKER_TIMEOUT"}
                            else exc.code
                        )
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

            async def finalize_candidate(self, state: Any, project_root: str) -> Any | None:
                """Gate direct completion through the existing Verification OS.

                P0-P.  This adapter has always declared
                ``verification_required = True``, but before this method existed
                the runner's accept gate at ``runner.py`` is guarded by
                ``hasattr(integration_adapter, "finalize_candidate")``.  That
                guard is False here, so the runner marked the task
                ``passed=True`` with reason ``canonical_acceptance_deferred`` and
                never invoked the deferral target: acceptance was *assumed*.

                The authority is unchanged.  This routes into the same
                ``VerificationEngine`` that ``CanonicalWorkerAdapter`` uses; it
                does not introduce a second verifier, and it does not touch
                ``_decide_goal_completion`` or ``ExecutionRecord._finish``.

                The verdict is produced from the record's own observed outcome,
                so a failing direct command cannot be accepted.  The five
                statuses are recorded separately rather than merged, because
                ``ExecutionRecord`` and ``GoalRun`` are still two terminal
                authorities and collapsing them here would hide P0-O-G3.
                """
                # Mirrors CanonicalWorkerAdapter's own guard. The flag is a
                # property of this adapter, not of the record.
                if not type(self).verification_required:
                    return None

                from runtime.verification.engine import VerificationEngine
                from runtime.verification.models import (
                    AcceptanceCriterion,
                    EvidenceItem,
                    VerificationSpec,
                )
                from server.goal_run.git_diff import current_head

                task_id = record.goal_task_id or record.task_id
                head_sha = current_head(project_root)
                criterion = AcceptanceCriterion(
                    id="ac-direct-command-observed",
                    description=(
                        "the direct command reached the verdict it claims: exit code "
                        "and outcome recorded by the governed runner"
                    ),
                    required=True,
                )
                spec = VerificationSpec.create_for_task(
                    task_id=task_id,
                    goal_run_id=state.goal_id,
                    head_sha=head_sha,
                    acceptance_criteria=[criterion],
                )
                engine = VerificationEngine(project_root)
                bundle = await engine.collect_evidence_bundle(
                    task_id, state.goal_id, head_sha, spec
                )

                # The observation is the command's own outcome, not a
                # restatement of the task text.
                #
                # Ordering matters: finalize_candidate runs INSIDE
                # project_run_goal, which is awaited at :3076, while _finish
                # only runs afterwards at :3113. So record.status is still
                # RUNNING here and says nothing about the verdict. The record's
                # exit_code and direct_status are set by the direct runner before
                # the leaf returns, so those are the observed outcome.
                #
                # exit_code is carried explicitly because
                # run_independent_verifier treats a non-zero exit_code as
                # explicit failure evidence.
                exit_code = record.exit_code
                # R3: fail closed. Deriving failure from exit_code alone reported
                # a spawn failure as PASS, because a command that never started has
                # no exit code at all (exit_code None, failure_class set). Success is
                # now the only thing that can produce a passing verdict.
                # A missing exit code is NOT itself a failure: some successful
                # paths (veya.mission.run among them) never produce one, and
                # treating that as failure broke them. Failure is the presence of a
                # positive failure signal -- a non-zero exit, a failure class, or a
                # direct status that is not a success. The spawn failure this was
                # written for has no exit code but does carry a failure class.
                record_failed = (
                    (exit_code is not None and exit_code != 0)
                    or bool(record.failure_class)
                    or str(getattr(record, "direct_status", "") or "") in
                    {"failed", "error", "blocked"}
                )
                bundle = bundle.add_evidence(
                    EvidenceItem(
                        id="direct-command-observed",
                        kind="failure" if record_failed else "test_result",
                        source="remote.direct_command",
                        content=json.dumps(
                            {
                                "tool": record.tool,
                                "command": record.command,
                                "exit_code": exit_code,
                                "status": str(record.status),
                                "failure_class": record.failure_class,
                                "result_summary": record.result_summary,
                            },
                            ensure_ascii=False,
                            default=str,
                        ),
                        producer="remote",
                        metadata={
                            "criterion_id": criterion.id,
                            "goal_run_id": state.goal_id,
                            "bot_id": state.bot_id,
                            "exit_code": exit_code,
                            "failed": record_failed,
                            "failure_class": record.failure_class,
                            "direct_status": getattr(record, "direct_status", None),
                            "execution_id": record.execution_id,
                        },
                    )
                )
                bundle = bundle.scoped_to(state.bot_id)
                verdict = await engine.run_independent_verifier(spec, bundle, head_sha)

                # P0-O-G3: record each authority's outcome separately. The
                # ExecutionRecord status is NOT overwritten here — this adapter
                # observes it; only GoalRun reacts to the verdict.
                record.verification_result = verdict.outcome
                record.verification_summary = verdict.summary
                record.events.append(
                    {
                        "ts": time.time(),
                        "kind": "direct_verification",
                        "phase": record.phase,
                        "verdict": verdict.outcome,
                        "summary": verdict.summary,
                        # The record is not terminal yet at this point (see the
                        # ordering note above), so this is the status the
                        # ExecutionRecord authority WILL settle on, recorded
                        # separately from the verdict. P0-O-G3 keeps the two
                        # authorities visible rather than merged.
                        "execution_record_status": str(record.status),
                        "observed_exit_code": exit_code,
                        "direct_status": record.direct_status,
                    }
                )
                manager._persist(record)
                return verdict

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
            elif record.failure_class in {
                "EXECUTION_TIMEOUT",
                str(ExecutionFailureClass.TOOL_TIMEOUT),
                str(ExecutionFailureClass.PROCESS_TIMEOUT),
            }:
                self._finish(
                    record,
                    str(ExecutionStatus.TIMED_OUT),
                    message=record.failure_detail or "execution timed out",
                    error=record.failure_class,
                )
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

    def transition(self, record: ExecutionRecord, target: str, *, via: str | None = None) -> None:
        """Move a record to ``target`` only if the state machine allows it.

        An illegal move is refused and recorded. The record keeps the state it
        had, and an ``ILLEGAL_TRANSITION`` event explains the refusal, so a late
        or buggy writer can never quietly downgrade a finished execution into a
        failure — and never quietly upgrade one either.
        """

        current = str(record.status)
        try:
            assert_legal_transition(via or current, target)
        except IllegalTransition as exc:
            record.events.append(
                {
                    "ts": time.time(),
                    "kind": "ILLEGAL_TRANSITION",
                    "phase": current,
                    "message": f"{current} -> {target} refused: {exc}",
                }
            )
            self._persist(record)
            raise
        record.status = target
        self._persist(record)
        self._execution_event_store(record).append(
            ExecutionEventType.EXECUTION_STATE_CHANGED,
            payload={"kind": "status", "status": target, "phase": record.phase},
        )

    def _finish(
        self,
        record: ExecutionRecord,
        status: str,
        *,
        message: str | None = None,
        error: str | None = None,
    ) -> None:
        self.transition(record, status)
        record.phase = status
        record.lifecycle_state = (
            "TIMED_OUT"
            if status == str(ExecutionStatus.FAILED)
            and record.failure_class
            in {
                "EXECUTION_TIMEOUT",
                str(ExecutionFailureClass.TOOL_TIMEOUT),
                str(ExecutionFailureClass.PROCESS_TIMEOUT),
            }
            else status
        )
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
        qualification_checkpoint(
            "BEFORE_TERMINAL_PERSIST",
            execution_id=record.execution_id,
            dispatch_id=record.dispatch_id,
            goal_run_id=record.goal_run_id,
            goal_task_id=record.goal_task_id,
            status=status,
        )
        self._persist(record)
        self._execution_event_store(record).append(
            ExecutionEventType.EXECUTION_STATE_CHANGED,
            payload={"kind": "status", "status": status, "phase": status,
                     "message": str(record.message or status)[:400]},
        )
        qualification_checkpoint(
            "AFTER_TERMINAL_PERSIST",
            execution_id=record.execution_id,
            dispatch_id=record.dispatch_id,
            goal_run_id=record.goal_run_id,
            goal_task_id=record.goal_task_id,
            status=status,
        )
        # Reclaim the execution worktree on EVERY terminal path, not just
        # parents.  The old `parent_execution_id is None` gate meant that
        # worker.dispatch children -- which each own their own isolated
        # worktree -- never released it, leaking one git worktree per child
        # (measured: 585 leaked task-* worktrees, 692MB of .git/worktrees).
        #
        # teardown_worktree is fail-closed: it refuses dirty, locked, and
        # /proc-referenced worktrees, so an execution that still owns uncommitted
        # work keeps its worktree.  The outcome is counted, not swallowed.
        if record.worktree_path and not record.keep_worktree:
            try:
                from runtime.coding.worktree import teardown_worktree

                outcome = teardown_worktree(record.worktree_path, execution_status=str(status))
            except Exception as exc:  # never let cleanup break terminal persist
                outcome = {"cleaned": False, "status": "TEARDOWN_ERROR", "error": repr(exc)}
            if outcome.get("cleaned"):
                self.metrics_bump("worktrees_released", 1)
                record.worktree_released_at = time.time()
            else:
                self.metrics_bump("worktrees_retained", 1)
            self._persist(record)

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

    def lookup_dispatch(self, dispatch_id: str) -> ExecutionRecord | None:
        """Resolve the durable execution handle by its external idempotency key."""
        with self._lock:
            records = list(self._records.values())
        for record in records:
            if record.dispatch_id == dispatch_id:
                return self._lookup(record.execution_id)
        for record in self.store.load_all():
            if record.dispatch_id == dispatch_id:
                with self._lock:
                    self._records[record.execution_id] = record
                return record
        return None

    def _execution_event_store(self, record: ExecutionRecord) -> ExecutionEventStore:
        root = record.requested_realpath or record.requested_workspace or str(Path.cwd())
        return ExecutionEventStore.shared(root, record.execution_id)

    def _append_execution_event(
        self, record: ExecutionRecord, *, kind: str, message: str, phase: str | None = None
    ) -> None:
        event_type = ExecutionEventType.OUTPUT
        upper = kind.upper()
        if kind == "phase":
            event_type = ExecutionEventType.EXECUTION_STATE_CHANGED
        elif kind in {"STDOUT", "STDERR", "output"}:
            event_type = ExecutionEventType.OUTPUT
        elif "error" in upper or "fail" in upper:
            event_type = ExecutionEventType.ERROR
        elif kind == "warning":
            event_type = ExecutionEventType.WARNING
        elif kind == "tool":
            event_type = ExecutionEventType.TOOL_COMPLETED
        elif kind == "progress":
            event_type = ExecutionEventType.PROGRESS
        elif kind == "checkpoint":
            event_type = ExecutionEventType.CHECKPOINT_CREATED
        self._execution_event_store(record).append(
            event_type,
            payload={"kind": kind, "message": message, "phase": phase or record.phase,
                     "step": record.current_step, "total": record.total_steps},
        )

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
        self._append_execution_event(record, kind=kind, message=message, phase=phase)

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
        """Record the resolved checkout identity once it has been determined (P0-B)."""

        record = self._record_for_update(execution_id)
        record.worktree_path = str(worktree_path)
        record.worktree_repo_root = str(worktree_repo_root)
        self._persist(record)

    def set_admission(self, execution_id: str, decision: Any) -> None:
        """Record the admission decision without touching the lifecycle state."""

        record = self._record_for_update(execution_id)
        record.admission_status = str(decision.status)
        record.admission_reason = str(decision.reason)
        record.admission_failure_class = decision.failure_class
        self._persist(record)

    def set_timeout(
        self,
        execution_id: str,
        *,
        kind: str,
        seconds: float | None = None,
        attribution: ExecutionTimeoutAttribution | None = None,
    ) -> None:
        """Persist timeout provenance on the record.

        ``attribution`` is optional and additive. When present it is the
        structured form of the same fact; when absent the call is exactly what it
        was before, so no existing timeout path loses provenance by not knowing
        about this.
        """

        record = self._record_for_update(execution_id)
        record.timeout_type = str(kind)
        if seconds is not None:
            record.timeout_seconds = float(seconds)
        if record.last_progress_at is None:
            record.last_progress_at = record.last_output_at or record.started_at
        if attribution is not None:
            record.timeout_attribution = attribution.to_dict()
        self._persist(record)

    def attribute_timeout(
        self,
        execution_id: str,
        *,
        kind: str = TimeoutKind.EXECUTION,
        elapsed_s: float | None = None,
        at: float | None = None,
    ) -> ExecutionTimeoutAttribution:
        """Build and persist this execution's timeout attribution for one clock.

        The single place an :class:`ExecutionTimeoutAttribution` is written, so a
        receipt cannot claim a budget the record never declared. Elapsed time
        defaults to the record's own measurement rather than being passed in, so
        a caller cannot attribute a timeout it never actually hit.
        """

        record = self._record_for_update(execution_id)
        elapsed = record.elapsed_s if elapsed_s is None else float(elapsed_s)
        attribution = record.timeout_policy.attribute(
            kind, elapsed_s=elapsed, at=at if at is not None else time.time()
        )
        record.timeout_attribution = attribution.to_dict()
        self._persist(record)
        return attribution

    def set_target(
        self,
        execution_id: str,
        *,
        target_type: str,
        dirty_state: bool | None = None,
    ) -> None:
        """Record which execution target ran, and the state of that checkout.

        The resolved target is not derivable from the requested workspace: a
        request for the project root may legitimately run in an isolated
        worktree, and a request for a worktree may run in the canonical tree.
        Callers are entitled to see which happened.
        """

        record = self._record_for_update(execution_id)
        record.target_type = str(target_type)
        if dirty_state is not None:
            record.dirty_state = bool(dirty_state)
        self._persist(record)

    def set_finalization(self, execution_id: str, result: dict[str, Any]) -> None:
        """Persist finalizer evidence before the terminal execution decision.

        SF-RECEIPT.  A receipt that is present but not a valid receipt object is
        a contract failure, not an absent receipt.  Previously any non-dict was
        dropped without a word, which made an invalid receipt indistinguishable
        from no receipt and discarded the commit/promotion claims alongside it.
        """

        record = self._record_for_update(execution_id)
        record.finalization_status = str(result.get("status") or "")
        record.finalization_failure_class = result.get("failure_class")
        if "receipt" in result:
            problem = _receipt_contract_problem(result["receipt"])
            if problem is not None:
                # Explicit, decidable failure. No receipt is stored, so nothing
                # false is asserted, and the commit/promotion claims that rode in
                # on the same payload are withheld rather than half-applied.
                self._fail_receipt_contract(record, problem, source="finalizer")
                return
            receipt = result["receipt"]
            record.receipt_contract_status = "valid"
            record.receipt_contract_error = None
            record.effect_receipt = receipt
            record.execution_commit_sha = result.get("commit_sha")
            record.promotion_state = result.get("promotion_status")
            verification = receipt.get("verification") or {}
            promotion = verification.get("promotion")
            if isinstance(promotion, dict):
                record.canonical_after_sha = promotion.get("canonical_after_sha")
        else:
            # Absent is a distinct, recordable state. It is not a contract
            # failure: a task kind that legitimately has no receipt still says so.
            record.receipt_contract_status = "absent"
        self._persist(record)

    def set_effect_receipt(self, execution_id: str, receipt: dict[str, Any]) -> None:
        """Persist the worker effect receipt (tool/shell/file telemetry).

        READ tasks never reach the finalizer, so their receipt would otherwise
        be dropped.  This persists the telemetry for every task kind without
        triggering any commit/promotion side effect.

        SF-RECEIPT.  Validated like the finalizer's receipt.  Storing a
        non-mapping here used to survive until ``to_public`` tried
        ``dict(...)`` on it and raised, so a bad receipt surfaced as a
        serialization crash far from its cause.
        """

        record = self._record_for_update(execution_id)
        problem = _receipt_contract_problem(receipt)
        if problem is not None:
            self._fail_receipt_contract(record, problem, source="worker")
            return
        record.receipt_contract_status = "valid"
        record.receipt_contract_error = None
        record.effect_receipt = receipt
        self._persist(record)

    def _fail_receipt_contract(
        self, record: ExecutionRecord, problem: str, *, source: str
    ) -> None:
        """Record a receipt contract failure loudly and without a false receipt."""

        record.receipt_contract_status = "invalid"
        record.receipt_contract_error = f"{source}: {problem}"
        record.effect_receipt = None
        # Withhold any commit/promotion claim that arrived with the bad payload.
        record.execution_commit_sha = None
        record.promotion_state = None
        record.canonical_after_sha = None
        record.finalization_failure_class = "RECEIPT_CONTRACT_INVALID"
        record.events.append(
            {
                "ts": time.time(),
                "kind": "receipt_contract_failure",
                "phase": record.phase,
                "message": record.receipt_contract_error,
                "source": source,
            }
        )
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
            self._execution_event_store(record).append(
                ExecutionEventType.OUTPUT,
                payload={"stream": stream, "text": line[:400], "phase": record.phase,
                         "step": record.current_step, "total": record.total_steps},
            )
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
        stdout_truncated: bool | None = None,
        stderr_truncated: bool | None = None,
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
        # The record also clips to ``_DIRECT_TAIL_BYTES`` above, so truncation
        # is true if either the producer said so or this store had to cut.
        if stdout_truncated is not None:
            record.stdout_truncated = bool(stdout_truncated)
        elif stdout_tail is not None and len(stdout_tail) > _DIRECT_TAIL_BYTES:
            record.stdout_truncated = True
        if stderr_truncated is not None:
            record.stderr_truncated = bool(stderr_truncated)
        elif stderr_tail is not None and len(stderr_tail) > _DIRECT_TAIL_BYTES:
            record.stderr_truncated = True
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
        record = self._authorize_cancel(
            execution_id,
            token_id=token_id,
            session_id=session_id,
            workspace_realpath=workspace_realpath,
            principal=principal,
        )
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
        # Make the waypoint observable. The record is not CANCELLED yet: the
        # worker has been signalled but has not stopped, and reporting CANCELLED
        # here would be a completion claim we have not earned. The status stays
        # RUNNING so the phase gate still sees the legal RUNNING -> CANCELLED
        # edge when the worker actually stops.
        if not record.is_terminal:
            record.phase = str(ExecutionPhase.CANCEL_REQUESTED)
        record.cancellation_intent = {
            "execution_id": execution_id,
            "requested_at": time.time(),
            "session_id": session_id,
            "reason": "explicit_process_cancel",
        }
        self._persist(record)
        if record.process_group_id:
            with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
                os.killpg(record.process_group_id, 15)
        if record.parent_execution_id:
            with self._lock:
                task = self._tasks.get(execution_id)
            if task is not None and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
            if not record.is_terminal:
                self._finish(record, str(ExecutionStatus.CANCELLED), message="execution cancelled")
            return record
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

    def _authorize_cancel(
        self,
        execution_id: str,
        *,
        token_id: str,
        session_id: str | None,
        workspace_realpath: str | None,
        principal: str | None,
    ) -> ExecutionRecord:
        """Authorize lifecycle control without making a session the owner.

        The durable execution is the lifecycle authority. Session identity is
        retained for audit and same-session compatibility, while an authorized
        principal may control another session's execution only when the caller
        is bound to the same repository identity.
        """

        record = self._lookup(execution_id)
        if record is None:
            raise ExecutionError("NOT_FOUND", "unknown execution_id")
        caller = str(principal or "")
        privileged = caller in {"system", "admin"}
        owner = caller != "" and caller == str(record.principal or record.principal_id or "")
        token_owner = token_id == record.token_id
        if not (token_owner or privileged or owner):
            raise ExecutionError("TOOL_DENIED", "caller is not authorized to cancel execution")

        if workspace_realpath:
            from .workspace_binding import canonical

            caller_identity = canonical(workspace_realpath)
            own_identities = {
                canonical(candidate)
                for candidate in (
                    record.requested_realpath,
                    record.resolved_repo_root,
                    record.repo_identity,
                    record.worktree_path,
                    record.worktree_repo_root,
                )
                if candidate
            }
            if caller_identity not in own_identities:
                raise ExecutionError(
                    "WORKSPACE_DENIED",
                    "workspace/repository identity does not match execution",
                )
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
    "ExecutionTimeoutAttribution",
    "ExecutionTimeoutPolicy",
    "ExecutionType",
    "ProgressReporter",
    "Runner",
    "TimeoutKind",
    "direct_spawn_failure",
    "report_progress",
    "use_reporter",
]
