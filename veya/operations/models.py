"""Models for Veya Agent Operations V1 (spec §3, §4, §7, §8, §12, §13, §16, §19, §21, §22, §26, §28).

Operations V1 is the operational governance authority over lifecycle, health, SLOs,
alerting, quotas, cost budgets, rollouts, maintenance windows, and audits.
It maintains strict zero semantic authority, zero direct placement, and zero direct execution.
"""

from __future__ import annotations

import enum
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any


class AgentOperationalStatus(enum.StrEnum):
    ACTIVE = "ACTIVE"
    DRAINING = "DRAINING"
    PAUSED = "PAUSED"
    MAINTENANCE = "MAINTENANCE"
    DEGRADED = "DEGRADED"
    BLOCKED = "BLOCKED"
    DISABLED = "DISABLED"


class HealthState(enum.StrEnum):
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    UNHEALTHY = "UNHEALTHY"
    UNKNOWN = "UNKNOWN"


class ProviderOperationalStatus(enum.StrEnum):
    AVAILABLE = "AVAILABLE"
    DEGRADED = "DEGRADED"
    COOLDOWN = "COOLDOWN"
    UNAVAILABLE = "UNAVAILABLE"
    UNKNOWN = "UNKNOWN"


class RolloutStatus(enum.StrEnum):
    PENDING = "PENDING"
    IN_PROGRESS = "IN_PROGRESS"
    HEALTH_CHECK = "HEALTH_CHECK"
    HALTED = "HALTED"
    ROLLED_BACK = "ROLLED_BACK"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class AlertState(enum.StrEnum):
    OPEN = "OPEN"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    RESOLVED = "RESOLVED"


class IncidentStatus(enum.StrEnum):
    OPEN = "OPEN"
    MITIGATING = "MITIGATING"
    RESOLVED = "RESOLVED"
    POSTMORTEM_REQUIRED = "POSTMORTEM_REQUIRED"


class QuotaEnforcementAction(enum.StrEnum):
    BLOCK = "BLOCK"
    QUEUE = "QUEUE"
    THROTTLE = "THROTTLE"


class MaintenanceScope(enum.StrEnum):
    FLEET = "fleet"
    AGENT = "agent"
    PROVIDER = "provider"
    EXECUTOR = "executor"
    WORKSPACE = "workspace"


@dataclass
class OperationalPolicy:
    policy_id: str
    name: str
    rules: dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> OperationalPolicy:
        return cls(**data)


@dataclass
class AgentOperationalState:
    agent_id: str
    desired_state: AgentOperationalStatus = AgentOperationalStatus.ACTIVE
    observed_state: AgentOperationalStatus = AgentOperationalStatus.ACTIVE
    reason: str = ""
    runtime_generation: int = 1
    operations_generation: int = 1
    principal_id: str = "default"
    metadata: dict[str, Any] = field(default_factory=dict)
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["desired_state"] = self.desired_state.value
        d["observed_state"] = self.observed_state.value
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AgentOperationalState:
        data_copy = dict(data)
        data_copy["desired_state"] = AgentOperationalStatus(data_copy["desired_state"])
        data_copy["observed_state"] = AgentOperationalStatus(data_copy["observed_state"])
        return cls(**data_copy)


@dataclass
class FleetOperationalState:
    fleet_id: str
    operations_generation: int = 1
    desired_state: str = "ACTIVE"
    observed_state: str = "ACTIVE"
    active_maintenance_windows: list[str] = field(default_factory=list)
    active_rollout_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> FleetOperationalState:
        return cls(**data)


@dataclass
class MaintenanceWindow:
    id: str
    scope: MaintenanceScope
    target_id: str
    starts_at: float
    ends_at: float
    reason: str
    created_by: str
    policy: dict[str, Any] = field(default_factory=dict)
    status: str = "ACTIVE"
    created_at: float = field(default_factory=time.time)

    def is_active(self, now: float | None = None) -> bool:
        t = now if now is not None else time.time()
        return self.status == "ACTIVE" and self.starts_at <= t <= self.ends_at

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["scope"] = self.scope.value
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MaintenanceWindow:
        data_copy = dict(data)
        data_copy["scope"] = MaintenanceScope(data_copy["scope"])
        return cls(**data_copy)


@dataclass
class RolloutRevision:
    revision_id: str
    artifact_version: str
    runtime_generation: int
    created_at: float = field(default_factory=time.time)


@dataclass
class RolloutTarget:
    agent_id: str
    current_version: str
    target_version: str
    status: str = "PENDING"  # PENDING, DRAINING, DEPLOYING, HEALTHY, COMPLETED, FAILED


@dataclass
class RolloutPlan:
    rollout_id: str
    artifact_version: str
    target_selector: dict[str, Any]
    batch_size: int = 2
    max_unavailable: int = 1
    max_surge: int = 1
    health_gate: str = "STRICT"
    rollback_policy: str = "AUTOMATIC"
    runtime_generation: int = 1
    status: RolloutStatus = RolloutStatus.PENDING
    targets: list[RolloutTarget] = field(default_factory=list)
    rollback_revision: RolloutRevision | None = None
    rollback_reason: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["status"] = self.status.value
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RolloutPlan:
        data_copy = dict(data)
        data_copy["status"] = RolloutStatus(data_copy["status"])
        data_copy["targets"] = [RolloutTarget(**t) for t in data_copy.get("targets", [])]
        if data_copy.get("rollback_revision"):
            data_copy["rollback_revision"] = RolloutRevision(**data_copy["rollback_revision"])
        return cls(**data_copy)


@dataclass
class HealthPolicy:
    policy_id: str
    heartbeat_timeout_s: float = 30.0
    error_rate_threshold: float = 0.05
    latency_threshold_s: float = 10.0
    queue_depth_threshold: int = 50
    lease_failure_threshold: int = 3
    execution_failure_threshold: int = 5


@dataclass
class SLODefinition:
    slo_id: str
    metric: str  # availability, admission_latency, placement_latency, execution_start_latency, recovery_latency, queue_wait, mission_success
    target: float
    window_s: float = 3600.0
    scope: str = "fleet"
    evaluation_policy: str = "rolling"  # rolling or fixed


@dataclass
class SLOWindow:
    window_id: str
    starts_at: float
    ends_at: float
    samples: list[float] = field(default_factory=list)


@dataclass
class SLOEvaluation:
    slo_id: str
    sli_value: float
    slo_target: float
    error_budget_remaining: float
    breach: bool
    evaluated_at: float = field(default_factory=time.time)


@dataclass
class AlertRule:
    rule_id: str
    name: str
    metric: str
    threshold: float
    severity: str = "WARN"  # INFO, WARN, CRITICAL
    dedupe_key_template: str = "{rule_id}:{target}"
    cooldown_s: float = 300.0


@dataclass
class AlertInstance:
    alert_id: str
    rule_id: str
    dedupe_key: str
    severity: str
    target: str
    message: str
    first_seen: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)
    occurrence_count: int = 1
    state: AlertState = AlertState.OPEN
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["state"] = self.state.value
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AlertInstance:
        d = dict(data)
        d["state"] = AlertState(d["state"])
        return cls(**d)


@dataclass
class QuotaPolicy:
    policy_id: str
    principal_id: str
    scope: str = "principal"
    dimensions: dict[str, float] = field(default_factory=dict)
    # e.g. {"concurrent_missions": 10.0, "CPU": 16.0, "RAM": 64.0, "token_usage": 1000000.0}
    enforcement_action: QuotaEnforcementAction = QuotaEnforcementAction.BLOCK


@dataclass
class UsageRecord:
    record_id: str
    principal_id: str
    agent_id: str
    mission_id: str
    goal_run_id: str
    execution_id: str
    provider: str
    model: str
    resource_type: str
    quantity: float
    unit: str
    observed_at: float = field(default_factory=time.time)
    source_event_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CostBudget:
    budget_id: str
    principal_id: str
    scope: str = "principal"
    budget_amount: float = 100.0
    currency_or_unit: str = "USD"
    period: str = "monthly"
    warning_threshold: float = 0.80  # 80%
    hard_limit: float = 1.00  # 100%
    current_spend: float = 0.0


@dataclass
class BudgetEvaluation:
    budget_id: str
    current_spend: float
    budget_amount: float
    utilization_ratio: float
    warning_reached: bool
    hard_limit_reached: bool
    action_required: str | None = None


@dataclass
class OperationalIncident:
    incident_id: str
    severity: str
    scope: str
    opened_at: float = field(default_factory=time.time)
    resolved_at: float | None = None
    root_cause: str = ""
    affected_agents: list[str] = field(default_factory=list)
    affected_missions: list[str] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)
    status: IncidentStatus = IncidentStatus.OPEN

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["status"] = self.status.value
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> OperationalIncident:
        d = dict(data)
        d["status"] = IncidentStatus(d["status"])
        return cls(**d)


@dataclass
class OperationalAuditRecord:
    record_id: str = field(default_factory=lambda: f"audit_{uuid.uuid4().hex[:12]}")
    actor: str = "operator"
    action: str = ""
    target: str = ""
    before: dict[str, Any] = field(default_factory=dict)
    after: dict[str, Any] = field(default_factory=dict)
    reason: str = ""
    timestamp: float = field(default_factory=time.time)
    request_id: str = ""
    approval_id: str | None = None
    principal_id: str = "default"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> OperationalAuditRecord:
        return cls(**data)
