"""Stable data contracts for delegated execution.

The contracts are intentionally plain dataclasses.  They can be persisted in
JSONL/taskgraph projections without importing an LLM implementation or a
transport layer.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

from runtime.bot_scope import DEFAULT_BOT_ID

DelegateStatus = Literal["complete", "partial", "failed", "paused", "cancelled"]
StopReason = Literal[
    "completed",
    "final_answer",
    "submit_report",
    "max_turns",
    "max_attempts",
    "llm_error",
    "budget_exhausted",
    "wall_deadline",
    "context_limit_reached",
    "cross_turn_repetition",
    "repeated_tool_calls",
    "response_truncated",
    "exception",
    "cancelled",
    "paused",
    "permission_denied",
    "sandbox_failure",
    "acceptance_failed",
]

_STOP_REASON_ALIASES = {
    "success": "completed",
    "done": "completed",
    "max_rounds": "max_turns",
    "deadline_exceeded": "wall_deadline",
    "timeout": "wall_deadline",
    "budget_exceeded": "budget_exhausted",
    "budget_exhausted": "budget_exhausted",
    "failed": "exception",
    "error": "exception",
    "user_cancelled": "cancelled",
}

_SUCCESS_REASONS = frozenset({"completed", "final_answer", "submit_report"})
_PARTIAL_REASONS = frozenset(
    {
        "max_turns",
        "max_attempts",
        "budget_exhausted",
        "wall_deadline",
        "context_limit_reached",
        "cross_turn_repetition",
        "repeated_tool_calls",
        "response_truncated",
        "acceptance_failed",
    }
)
_FAILED_REASONS = frozenset({"llm_error", "exception", "permission_denied", "sandbox_failure"})


def normalize_stop_reason(value: object) -> str:
    """Return a canonical reason, or ``unknown`` for an unrecognised value."""
    raw = str(value or "").strip().lower().replace(" ", "_")
    raw = _STOP_REASON_ALIASES.get(raw, raw)
    return raw if raw in StopReason.__args__ else "unknown"


def classify_status(status: object, stop_reason: object) -> DelegateStatus:
    """Classify a result conservatively.

    Unknown stop reasons are partial by contract.  A child that produced work
    but stopped for a known non-success reason is also never promoted to
    complete merely because the adapter reported ``success``.
    """
    reason = normalize_stop_reason(stop_reason)
    if reason == "unknown":
        return "partial"
    if reason in _SUCCESS_REASONS:
        return "complete"
    if reason == "cancelled":
        return "cancelled"
    if reason == "paused":
        return "paused"
    if reason in _PARTIAL_REASONS:
        return "partial"
    if reason in _FAILED_REASONS:
        return "failed"
    candidate = str(status or "partial")
    return candidate if candidate in DelegateStatus.__args__ else "partial"


@dataclass
class Evidence:
    id: str
    kind: str
    source: str
    content: str
    producer: str
    confidence: float | None = None
    sha256: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class AcceptanceCriterion:
    """A deterministic acceptance item passed across delegate boundaries."""

    id: str
    description: str
    required: bool = True

    @classmethod
    def from_value(cls, value: object, *, index: int = 0) -> AcceptanceCriterion:
        if isinstance(value, cls):
            return value
        if isinstance(value, dict):
            return cls(
                id=str(value.get("id") or f"criterion-{index + 1}"),
                description=str(value.get("description") or value.get("summary") or ""),
                required=bool(value.get("required", True)),
            )
        return cls(id=f"criterion-{index + 1}", description=str(value or ""))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Assertion:
    id: str
    statement: str
    evidence_ids: list[str] = field(default_factory=list)
    producer: str = ""
    status: Literal["supported", "weak", "conflicting"] = "weak"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class AcceptanceResult:
    id: str
    status: str
    summary: str = ""
    evidence: list[str] = field(default_factory=list)
    required: bool = True

    @classmethod
    def from_value(cls, value: object) -> AcceptanceResult:
        if isinstance(value, cls):
            return value
        if isinstance(value, dict):
            return cls(
                id=str(value.get("id") or "criterion"),
                status=str(value.get("status") or "pending"),
                summary=str(value.get("summary") or value.get("description") or ""),
                evidence=list(value.get("evidence") or []),
                required=bool(value.get("required", True)),
            )
        return cls(id="criterion", status="pending", summary=str(value or ""))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ArtifactRef:
    path: str
    kind: str
    producer: str
    status: Literal["draft", "verified", "partial", "failed"] = "draft"
    sha256: str | None = None
    size_bytes: int | None = None
    evidence_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_value(cls, value: object, *, producer: str = "unknown") -> ArtifactRef:
        if isinstance(value, cls):
            return value
        if isinstance(value, dict):
            return cls(
                path=str(value.get("path") or ""),
                kind=str(value.get("kind") or "file"),
                producer=str(value.get("producer") or producer),
                status=value.get("status", "draft"),
                sha256=value.get("sha256"),
                size_bytes=value.get("size_bytes"),
                evidence_ids=list(value.get("evidence_ids") or []),
            )
        return cls(path=str(value or ""), kind="file", producer=producer)


@dataclass
class DelegateRequest:
    delegate_id: str
    parent_task_id: str
    parent_trace_id: str
    objective: str
    context_ref: str | None = None
    capability_scope: list[str] = field(default_factory=list)
    acceptance: list[AcceptanceCriterion | Any] = field(default_factory=list)
    depth: int = 0
    estimated_tokens: int = 0
    budget_usd: float | None = None
    timeout_s: int = 5400
    workspace: str = ""
    output_paths: list[str] = field(default_factory=list)
    deadline: datetime | None = None
    # P3-A: the bot that owns this delegate. Must match the runtime's bot.
    bot_id: str = DEFAULT_BOT_ID

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        if self.deadline is not None:
            value["deadline"] = self.deadline.isoformat()
        return value


@dataclass
class DelegateResult:
    delegate_id: str
    status: DelegateStatus
    stop_reason: str
    summary: str = ""
    evidence: list[Evidence] = field(default_factory=list)
    assertions: list[Assertion] = field(default_factory=list)
    artifacts: list[ArtifactRef] = field(default_factory=list)
    acceptance_results: list[AcceptanceResult] = field(default_factory=list)
    completed_work: list[str] = field(default_factory=list)
    unfinished_work: list[str] = field(default_factory=list)
    cost_usd: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    duration_ms: int = 0
    child_trace_id: str = ""
    error_class: str | None = None
    error_message: str | None = None

    def __post_init__(self) -> None:
        self.stop_reason = normalize_stop_reason(self.stop_reason)
        self.status = classify_status(self.status, self.stop_reason)
        self.evidence = [
            item if isinstance(item, Evidence) else Evidence(**item) for item in self.evidence
        ]
        self.assertions = [
            item if isinstance(item, Assertion) else Assertion(**item) for item in self.assertions
        ]
        self.artifacts = [ArtifactRef.from_value(item) for item in self.artifacts]
        self.acceptance_results = [
            AcceptanceResult.from_value(item) for item in self.acceptance_results
        ]

    @classmethod
    def from_mapping(
        cls,
        delegate_id: str,
        value: dict[str, Any],
        *,
        child_trace_id: str = "",
        producer: str = "delegate",
    ) -> DelegateResult:
        """Adapt existing AgentLoop/Hicode-shaped dictionaries."""
        summary = str(value.get("summary") or value.get("final_answer") or "")
        reason = value.get("stop_reason") or value.get("stop_kind") or value.get("error")
        return cls(
            delegate_id=delegate_id,
            status=value.get("status", "partial"),
            stop_reason=str(reason or "unknown"),
            summary=summary,
            evidence=list(value.get("evidence") or []),
            assertions=list(value.get("assertions") or []),
            artifacts=[
                ArtifactRef.from_value(item, producer=producer)
                for item in value.get("artifacts", [])
            ],
            acceptance_results=list(value.get("acceptance_results") or []),
            completed_work=list(value.get("completed_work") or []),
            unfinished_work=list(value.get("unfinished_work") or []),
            cost_usd=float(value.get("cost_usd") or 0.0),
            prompt_tokens=int(value.get("prompt_tokens") or 0),
            completion_tokens=int(value.get("completion_tokens") or 0),
            duration_ms=int(value.get("duration_ms") or 0),
            child_trace_id=str(value.get("child_trace_id") or child_trace_id),
            error_class=value.get("error_class"),
            error_message=value.get("error_message") or value.get("error"),
        )

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["evidence"] = [item.to_dict() for item in self.evidence]
        value["assertions"] = [item.to_dict() for item in self.assertions]
        value["artifacts"] = [item.to_dict() for item in self.artifacts]
        value["acceptance_results"] = [
            item.to_dict() if hasattr(item, "to_dict") else item for item in self.acceptance_results
        ]
        return value


@dataclass
class SpawnBudget:
    max_depth: int = 2
    max_parallel: int = 4
    max_tokens: int = 300_000
    max_cost_usd: float | None = None
    root_wall_time_s: int = 7200
    subagent_timeout_s: int = 5400


@dataclass
class SharedTaskContext:
    """Minimal context package shared with a child, not a copied parent history."""

    objective: str
    constraints: list[str] = field(default_factory=list)
    acceptance: list[AcceptanceCriterion | Any] = field(default_factory=list)
    completed_work: list[str] = field(default_factory=list)
    evidence_refs: list[str] = field(default_factory=list)
    artifact_refs: list[str] = field(default_factory=list)
    workspace_ref: str = ""

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["acceptance"] = [
            item.to_dict() if hasattr(item, "to_dict") else item for item in self.acceptance
        ]
        return value


@dataclass
class SpawnReservation:
    job_id: str
    depth: int
    estimated_tokens: int
    estimated_cost_usd: float
    acquired: bool = False
    acquired_at: float = 0.0
    released: bool = False


@dataclass
class ExecutionSchedulerState:
    running: dict[str, Any] = field(default_factory=dict)
    queued: list[str] = field(default_factory=list)
    completed: set[str] = field(default_factory=set)
    failed: set[str] = field(default_factory=set)
    available_slots: int = 0
    finalizing: bool = False


@dataclass
class ExecutionCheckpoint:
    event_cursor: str
    scheduler_snapshot: dict[str, Any]
    running_delegate_ids: list[str] = field(default_factory=list)
    completed_task_ids: list[str] = field(default_factory=list)
    pending_task_ids: list[str] = field(default_factory=list)
    artifact_manifest_ref: str | None = None
    finalization_started: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ArtifactManifest:
    task_id: str
    artifacts: list[ArtifactRef] = field(default_factory=list)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "artifacts": [item.to_dict() for item in self.artifacts],
            "created_at": self.created_at.isoformat(),
        }


# P2-A §6: Parallel delegation + fan-in
# MasterAgent may spawn N semantic delegates, but ALL physical execution
# must funnel through the canonical path: CanonicalActionRequest → GoalRun →
# ActionGateway.  Authority count is strictly 1 — no second execution
# authority is allowed.

PARALLEL_SEMANTIC_DELEGATION = True
"""P2-A: allow MasterAgent to spawn N semantic delegates."""

PARALLEL_EXECUTION_AUTHORITY_COUNT = 1
"""P2-A: total execution authority count — never exceed 1."""


def reconcile_delegate_results(
    results: list[DelegateResult],
) -> dict[str, Any]:
    """Fan-in: reconcile multiple DelegateResults into one decision.

    Returns a dict with:
    - overall_status: "success" | "partial" | "failed" | "blocked"
    - complete_count: number of complete results
    - partial_count: number of partial results
    - failed_count: number of failed results
    - blocked_count: number of blocked results (self-report not verified)
    - evidence: merged evidence items (deduplicated by sha256)
    - assertions: merged assertions (deduplicated by statement)
    - rationale: human-readable reconciliation reason

    Key invariant: a worker's self-report is never treated as verified success.
    Only results that completed through the canonical ActionGateway path
    contribute to ``overall_status`` = ``"success"``.
    """
    if not results:
        return {
            "overall_status": "blocked",
            "complete_count": 0,
            "partial_count": 0,
            "failed_count": 0,
            "blocked_count": 0,
            "evidence": [],
            "assertions": [],
            "rationale": "no delegate results provided",
        }

    partial_count = sum(1 for r in results if r.status == "partial")
    failed_count = sum(1 for r in results if r.status == "failed")
    blocked_count = sum(1 for r in results if r.status == "blocked")

    # Merge evidence (deduplicate by sha256)
    evidence_by_key: dict[str, Any] = {}
    for r in results:
        for item in r.evidence:
            key = item.sha256 or json.dumps(
                {"source": item.source, "content": item.content}, sort_keys=True
            )
            evidence_by_key.setdefault(key, item)

    # Merge assertions (deduplicate by normalized statement)
    assertions_by_key: dict[str, Any] = {}
    for r in results:
        for a in r.assertions:
            stmt = (
                a.statement.strip().lower() if hasattr(a, "statement") else str(a).strip().lower()
            )
            assertions_by_key.setdefault(stmt, a)

    # Determine overall status (excluding self-reports from "success" count)
    non_self_report_complete = sum(
        1 for r in results if r.status == "complete" and not getattr(r, "self_report", False)
    )

    if non_self_report_complete > 0 and failed_count == 0:
        overall_status = "success"
    elif partial_count > 0:
        overall_status = "partial"
    elif failed_count > 0:
        overall_status = "failed"
    else:
        overall_status = "blocked"

    # Rationale
    rationale_parts = []
    if non_self_report_complete > 0:
        rationale_parts.append(f"{non_self_report_complete} non-self-report complete result(s)")
    if partial_count > 0:
        rationale_parts.append(f"{partial_count} partial result(s)")
    if failed_count > 0:
        rationale_parts.append(f"{failed_count} failed result(s)")
    if blocked_count > 0:
        rationale_parts.append(f"{blocked_count} blocked result(s) (self-reports excluded)")

    rationale = "; ".join(rationale_parts) if rationale_parts else "no actionable results"

    return {
        "overall_status": overall_status,
        "complete_count": non_self_report_complete,
        "partial_count": partial_count,
        "failed_count": failed_count,
        "blocked_count": blocked_count,
        "evidence": list(evidence_by_key.values()),
        "assertions": [a for a in assertions_by_key.values() if a is not None],
        "rationale": rationale,
    }


# P2-A §7: Acceptance authority closure
# DelegateResult / playbook result / routine result are worker execution
# outcomes only.  Final acceptance is exclusively:
#   candidate.complete → EvidenceBundle → IndependentVerifier → verdict.
#  No delegated/playbook/routine result may claim to be the acceptance verdict.

DELEGATE_RESULT_ACCEPTANCE_AUTHORITY = 0
"""P2-A: DelegateResult has zero acceptance authority."""

PLAYBOOK_ACCEPTANCE_AUTHORITY = 0
"""P2-A: Playbook results have zero acceptance authority."""

ROUTINE_ACCEPTANCE_AUTHORITY = 0
"""P2-A: Routine results have zero acceptance authority."""

SUBAGENT_SELF_REPORT_AUTHORITY = 0
"""P2-A: Subagent self-report has zero acceptance authority."""

WORKER_SELF_REPORT_AUTHORITY = 0
"""P2-A: Worker self-report has zero acceptance authority."""

SECOND_ACCEPTANCE_AUTHORITY = 0
"""P2-A: No second acceptance authority beyond IndependentVerifier."""

SECOND_EXECUTION_AUTHORITY = 0
"""P2-A: No second execution authority beyond the canonical GoalRun path."""

SKILL_EXECUTION_AUTHORITY = 0
"""P2-B: skills describe how to do work; they never execute it."""

SKILL_ACCEPTANCE_AUTHORITY = 0
"""P2-B: skills never accept work; only IndependentVerifier verdicts accept."""

PLAYBOOK_EXECUTION_AUTHORITY = 0
"""P2-B: playbooks order steps; they never execute them."""

ROUTINE_EXECUTION_AUTHORITY = 0
"""P2-C: routines trigger only; they never execute."""


def assert_delegate_result_acceptance_authority_zero(
    delegate_result: DelegateResult,
) -> None:
    """Assert that a DelegateResult does not claim acceptance authority."""
    assert DELEGATE_RESULT_ACCEPTANCE_AUTHORITY == 0, (
        "DELEGATE_RESULT_ACCEPTANCE_AUTHORITY must stay 0"
    )


def assert_playbook_acceptance_authority_zero(
    playbook_result: dict[str, Any],
) -> None:
    """Assert that a playbook result does not claim acceptance authority."""
    assert PLAYBOOK_ACCEPTANCE_AUTHORITY == 0, "PLAYBOOK_ACCEPTANCE_AUTHORITY must stay 0"


def assert_routine_acceptance_authority_zero(
    routine_result: dict[str, Any],
) -> None:
    """Assert that a routine result does not claim acceptance authority."""
    assert ROUTINE_ACCEPTANCE_AUTHORITY == 0, "ROUTINE_ACCEPTANCE_AUTHORITY must stay 0"


def assert_subagent_self_report_authority_zero(
    flag: bool,
) -> None:
    """Assert that a subagent self-report is not treated as verified success."""
    if flag:
        raise AssertionError(
            "SUBAGENT_SELF_REPORT_AUTHORITY=0: subagent self-report "
            "cannot be treated as verified success"
        )


def assert_worker_self_report_authority_zero(
    flag: bool,
) -> None:
    """Assert that a worker self-report is not treated as verified success."""
    if flag:
        raise AssertionError(
            "WORKER_SELF_REPORT_AUTHORITY=0: worker self-report "
            "cannot be treated as verified success"
        )


def assert_second_acceptance_authority_zero(count: int = SECOND_ACCEPTANCE_AUTHORITY) -> None:
    """Assert that no second acceptance authority exists."""
    if count != 0:
        raise AssertionError(
            "SECOND_ACCEPTANCE_AUTHORITY=0: only IndependentVerifier may "
            "issue the final acceptance verdict"
        )


def assert_skill_execution_authority_zero() -> None:
    """Assert that skills never execute (P2-B)."""
    assert SKILL_EXECUTION_AUTHORITY == 0, "SKILL_EXECUTION_AUTHORITY must stay 0"


def assert_skill_acceptance_authority_zero() -> None:
    """Assert that skills never accept (P2-B)."""
    assert SKILL_ACCEPTANCE_AUTHORITY == 0, "SKILL_ACCEPTANCE_AUTHORITY must stay 0"


def assert_playbook_execution_authority_zero() -> None:
    """Assert that playbooks never execute (P2-B)."""
    assert PLAYBOOK_EXECUTION_AUTHORITY == 0, "PLAYBOOK_EXECUTION_AUTHORITY must stay 0"


def assert_routine_execution_authority_zero() -> None:
    """Assert that routines never execute (P2-C)."""
    assert ROUTINE_EXECUTION_AUTHORITY == 0, "ROUTINE_EXECUTION_AUTHORITY must stay 0"


def assert_second_execution_authority_zero(count: int = SECOND_EXECUTION_AUTHORITY) -> None:
    """Assert that no second execution authority exists."""
    if count != 0:
        raise AssertionError(
            "SECOND_EXECUTION_AUTHORITY=0: all physical execution must funnel "
            "through CanonicalActionRequest → GoalRun → ActionGateway"
        )


def assert_parallel_execution_authority_single(
    count: int = PARALLEL_EXECUTION_AUTHORITY_COUNT,
) -> None:
    """Assert the single canonical execution authority is never duplicated."""
    if count != 1:
        raise AssertionError(
            "PARALLEL_EXECUTION_AUTHORITY_COUNT must stay 1: N semantic delegates, "
            "exactly one execution authority"
        )


# P2-A §9: Subagent enforcement
SUBAGENT_ACCEPTANCE_AUTHORITY = 0
"""P2-A: subagent acceptance has zero authority."""


def assert_subagent_acceptance_authority_zero() -> None:
    """Assert that subagent acceptance has zero authority."""
    assert SUBAGENT_ACCEPTANCE_AUTHORITY == 0, "SUBAGENT_ACCEPTANCE_AUTHORITY must stay 0"


SUBAGENT_EXECUTION_AUTHORITY = 0
"""P2-A: subagent execution has zero authority."""


def assert_subagent_execution_authority_zero() -> None:
    """Assert that subagent execution has zero authority."""
    assert SUBAGENT_EXECUTION_AUTHORITY == 0, "SUBAGENT_EXECUTION_AUTHORITY must stay 0"


SUBAGENT_DIRECT_PHYSICAL_EXECUTION = 0
"""P2-A: subagent cannot directly execute physical actions."""

SUBAGENT_GOALRUN_CREATION = 0
"""P2-A: subagent cannot create a new GoalRun."""


def assert_subagent_direct_physical_execution_zero(
    direct_physical_calls: int = 0,
) -> None:
    """Assert that a subagent does not directly execute physical actions.

    Physical execution must funnel through the canonical path:
    CanonicalActionRequest → GoalRun → ActionGateway.
    A subagent must not directly call the physical executor.
    """
    if direct_physical_calls != 0:
        raise AssertionError(
            "SUBAGENT_DIRECT_PHYSICAL_EXECUTION=0: subagent cannot directly "
            "execute physical actions; must funnel through CanonicalActionRequest "
            "→ GoalRun → ActionGateway"
        )


def assert_subagent_goalrun_creation_zero(
    created_goal_runs: int = 0,
) -> None:
    """Assert that a subagent does not create a new GoalRun.

    All delegation must happen within the existing GoalRun.
    A subagent must not create a new GoalRun.
    """
    if created_goal_runs != 0:
        raise AssertionError(
            "SUBAGENT_GOALRUN_CREATION=0: subagent cannot create a new GoalRun; "
            "all delegation must occur within the existing GoalRun"
        )
