"""Stable data contracts for delegated execution.

The contracts are intentionally plain dataclasses.  They can be persisted in
JSONL/taskgraph projections without importing an LLM implementation or a
transport layer.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

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
        self.evidence = [item if isinstance(item, Evidence) else Evidence(**item) for item in self.evidence]
        self.assertions = [
            item if isinstance(item, Assertion) else Assertion(**item) for item in self.assertions
        ]
        self.artifacts = [ArtifactRef.from_value(item) for item in self.artifacts]
        self.acceptance_results = [AcceptanceResult.from_value(item) for item in self.acceptance_results]

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
            artifacts=[ArtifactRef.from_value(item, producer=producer) for item in value.get("artifacts", [])],
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
