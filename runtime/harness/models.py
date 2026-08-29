"""Stable data contracts for the Veya production Agent harness.

These records are deliberately product-layer objects.  They are independent
of ``runtime.execution.models`` so the harness cannot change the Execution
Runtime ABI while adding coding context and evidence.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal, cast


def utc_now() -> datetime:
    return datetime.now(UTC)


def _serialize(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, list):
        return [_serialize(item) for item in value]
    if isinstance(value, dict):
        return {key: _serialize(item) for key, item in value.items()}
    return value


@dataclass
class GuideRule:
    id: str
    text: str
    category: str
    source_path: str
    source_line: int | None
    priority: int
    verifiable: bool

    def to_dict(self) -> dict[str, Any]:
        return cast(dict[str, Any], _serialize(asdict(self)))


@dataclass
class GuideCommands:
    build: list[str] = field(default_factory=list)
    test: list[str] = field(default_factory=list)
    lint: list[str] = field(default_factory=list)
    typecheck: list[str] = field(default_factory=list)
    format: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, list[str]]:
        return asdict(self)

    def all(self) -> dict[str, list[str]]:
        return {
            "build": list(self.build),
            "test": list(self.test),
            "lint": list(self.lint),
            "typecheck": list(self.typecheck),
            "format": list(self.format),
        }


@dataclass
class AntiPattern:
    id: str
    text: str
    source_path: str
    source_line: int | None
    category: str = "general"

    def to_dict(self) -> dict[str, Any]:
        return cast(dict[str, Any], _serialize(asdict(self)))


@dataclass
class GuideConflict:
    left_rule_id: str
    right_rule_id: str
    message: str
    source_paths: list[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ProjectGuide:
    workspace_id: str
    source_path: str
    rules: list[GuideRule] = field(default_factory=list)
    commands: GuideCommands = field(default_factory=GuideCommands)
    anti_patterns: list[AntiPattern] = field(default_factory=list)
    last_loaded_at: datetime = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return cast(dict[str, Any], _serialize(asdict(self)))


SensorKind = Literal[
    "lint",
    "test",
    "typecheck",
    "build",
    "schema",
    "security",
    "llm_judge",
]
SensorCost = Literal["free", "low", "medium", "high"]


@dataclass
class Sensor:
    id: str
    name: str
    kind: SensorKind
    command: str | None
    deterministic: bool
    cost_level: SensorCost
    required: bool
    timeout_s: int

    def __post_init__(self) -> None:
        # A model judgement is advisory by contract.  Coercing this one bit
        # keeps an untrusted registration from making acceptance non-terminal.
        if self.kind == "llm_judge":
            self.required = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SensorResult:
    sensor_id: str
    status: Literal["passed", "failed", "skipped", "error"]
    exit_code: int | None
    output_ref: str | None
    evidence_ids: list[str] = field(default_factory=list)
    duration_ms: int = 0
    message: str = ""
    command: str | None = None
    required: bool = False
    deterministic: bool = False
    started_at: str | None = None
    completed_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class RatchetCandidate:
    id: str
    workspace_id: str
    source_task_id: str
    failure_class: str
    observed_failure: str
    proposed_fix_layer: Literal["guide", "sensor", "permission", "tool", "test"]
    proposed_rule: str | None
    proposed_sensor: Sensor | None
    evidence_ids: list[str]
    status: Literal["candidate", "approved", "rejected", "applied"] = "candidate"
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
    applied_path: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return cast(dict[str, Any], _serialize(asdict(self)))


@dataclass
class CodingHarnessContract:
    workspace_id: str
    guide_refs: list[str]
    required_sensors: list[str]
    optional_sensors: list[str]
    permission_profile: str
    observability_profile: str
    memory_scope: str
    artifact_policy: str
    created_at: datetime = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return cast(dict[str, Any], _serialize(asdict(self)))

    def answers(self) -> dict[str, Any]:
        return {
            "guides_read": list(self.guide_refs),
            "required_sensors": list(self.required_sensors),
            "optional_sensors": list(self.optional_sensors),
            "allowed_writes": "task worktree only",
            "approval_policy": self.permission_profile,
            "artifact_path": self.artifact_policy,
            "verified_state": "required sensors passed and acceptance evidence is present",
            "failure_policy": "create an evidence-backed RatchetCandidate; never auto-apply",
        }


@dataclass
class HarnessCheck:
    name: str
    status: Literal["pass", "degraded", "blocked"]
    detail: str = ""

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass
class HarnessDoctorReport:
    status: Literal["HARNESS_READY", "HARNESS_DEGRADED", "HARNESS_BLOCKED"]
    workspace_path: str
    checks: list[HarnessCheck] = field(default_factory=list)
    degraded_reasons: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    evidence_run_id: str | None = None
    sensor_report_path: str | None = None
    verification_report_path: str | None = None

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["checks"] = [check.to_dict() for check in self.checks]
        return value


__all__ = [
    "AntiPattern",
    "CodingHarnessContract",
    "GuideCommands",
    "GuideConflict",
    "GuideRule",
    "HarnessCheck",
    "HarnessDoctorReport",
    "ProjectGuide",
    "RatchetCandidate",
    "Sensor",
    "SensorResult",
]
