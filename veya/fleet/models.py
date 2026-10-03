"""Fleet Data Models (spec §3-§13, §25, §31, §35, §41, §46).

Invariants:
- Fleet is placement / capacity / isolation / coordination authority only.
- MasterAgent remains the sole semantic authority within a Mission.
- GoalRun remains the durable execution authority.
- No second semantic authority, no second planner, no second goal engine.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any


class FleetStatus(StrEnum):
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    DEGRADED = "DEGRADED"
    DRAINING = "DRAINING"
    STOPPED = "STOPPED"


class AgentInstanceStatus(StrEnum):
    STARTING = "STARTING"
    READY = "READY"
    BUSY = "BUSY"
    WAITING = "WAITING"
    DRAINING = "DRAINING"
    UNAVAILABLE = "UNAVAILABLE"
    RECOVERING = "RECOVERING"
    STOPPED = "STOPPED"


class AdmissionOutcome(StrEnum):
    ADMIT = "ADMIT"
    DEFER = "DEFER"
    REJECT = "REJECT"


class LeaseStatus(StrEnum):
    ACTIVE = "ACTIVE"
    RELEASED = "RELEASED"
    EXPIRED = "EXPIRED"
    REVOKED = "REVOKED"


class ResourceType(StrEnum):
    CPU = "CPU"
    RAM = "RAM"
    GPU = "GPU"
    EXECUTOR_SLOT = "EXECUTOR_SLOT"
    WORKSPACE_SLOT = "WORKSPACE_SLOT"
    NETWORK_SLOT = "NETWORK_SLOT"
    PROVIDER_QUOTA = "PROVIDER_QUOTA"
    CUSTOM = "CUSTOM"


class ReservationStatus(StrEnum):
    RESERVED = "RESERVED"
    ACTIVE = "ACTIVE"
    RELEASED = "RELEASED"
    EXPIRED = "EXPIRED"


class MigrationStatus(StrEnum):
    REQUESTED = "REQUESTED"
    QUIESCING = "QUIESCING"
    SNAPSHOT_VALIDATED = "SNAPSHOT_VALIDATED"
    REBINDING = "REBINDING"
    RECOVERING = "RECOVERING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class AgentRole(StrEnum):
    PRIMARY = "PRIMARY"
    WORKER = "WORKER"
    REVIEWER = "REVIEWER"
    OBSERVER = "OBSERVER"
    SPECIALIST = "SPECIALIST"


class SchedulingPolicy(StrEnum):
    FIFO = "FIFO"
    PRIORITY_FIFO = "PRIORITY_FIFO"
    LEAST_LOADED = "LEAST_LOADED"
    CAPABILITY_AWARE = "CAPABILITY_AWARE"
    LOCALITY_AWARE = "LOCALITY_AWARE"


@dataclass
class AgentFleet:
    """Fleet entity describing capacity, generation and operational status (spec §3)."""

    fleet_id: str
    name: str = "default_fleet"
    status: FleetStatus = FleetStatus.STARTING
    generation: int = 1
    policy_ref: str = "default_policy"
    capacity_profile: dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["status"] = self.status.value
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AgentFleet:
        data = dict(data)
        if isinstance(data.get("status"), str):
            data["status"] = FleetStatus(data["status"])
        return cls(**data)


class AgentInstanceKind(StrEnum):
    PRIMARY = "PRIMARY"
    SUBAGENT = "SUBAGENT"
    BACKGROUND = "BACKGROUND"
    TEAM = "TEAM"
    TEMPORARY = "TEMPORARY"


@dataclass
class AgentInstance:
    """Agent runtime carrier instance (spec §4, §39). Not a semantic authority.

    P1-01 spec fields: agent_id, kind, status, parent_agent_id?, family_id?,
    goal_scope, capabilities, context_policy, created_at, last_active_at.
    """

    agent_instance_id: str
    fleet_id: str
    principal_id: str = "system"
    runtime_id: str = "default_runtime"
    workspace_scope: list[str] = field(default_factory=list)
    capability_profile: list[str] = field(default_factory=list)
    resource_class: str = "standard"
    placement: str = "local"
    status: AgentInstanceStatus = AgentInstanceStatus.READY
    generation: int = 1
    created_at: float = field(default_factory=time.time)
    last_heartbeat_at: float = field(default_factory=time.time)
    host_id: str = "localhost"
    provider: str = "default_provider"
    gpu_id: str = ""
    workspace_device: str = "disk0"
    kind: AgentInstanceKind = AgentInstanceKind.PRIMARY
    parent_agent_id: str | None = None
    family_id: str | None = None
    goal_scope: str = ""
    capabilities: list[str] = field(default_factory=list)
    context_policy: str = "default"
    last_active_at: float = field(default_factory=time.time)
    failure_domain: str = "zone-a"

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["status"] = self.status.value
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AgentInstance:
        data = dict(data)
        if isinstance(data.get("status"), str):
            data["status"] = AgentInstanceStatus(data["status"])
        return cls(**data)


@dataclass
class PlacementRequest:
    """Placement request submitted to Fleet (spec §7)."""

    mission_id: str
    principal_id: str
    required_capabilities: list[str] = field(default_factory=list)
    workspace_requirements: dict[str, Any] = field(default_factory=dict)
    resource_requirements: dict[str, Any] = field(default_factory=dict)
    isolation_requirements: dict[str, Any] = field(default_factory=dict)
    preferred_runtime: str = ""
    locality: str = ""
    priority: int = 0
    requested_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PlacementRequest:
        return cls(**data)


@dataclass
class PlacementDecision:
    """Placement decision made by FleetScheduler (spec §7, §43)."""

    placement_id: str
    mission_id: str
    agent_instance_id: str
    runtime_id: str
    reason: str
    manifest_snapshot: dict[str, Any] = field(default_factory=dict)
    resource_reservation_id: str = ""
    created_at: float = field(default_factory=time.time)
    generation: int = 1
    status: str = "ACTIVE"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PlacementDecision:
        return cls(**data)


@dataclass
class AgentLease:
    """Durable agent lease binding a Mission to an AgentInstance (spec §10)."""

    lease_id: str
    agent_instance_id: str
    mission_id: str
    holder_generation: int
    acquired_at: float = field(default_factory=time.time)
    heartbeat_at: float = field(default_factory=time.time)
    expires_at: float = field(default_factory=lambda: time.time() + 60.0)
    status: LeaseStatus = LeaseStatus.ACTIVE

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["status"] = self.status.value
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AgentLease:
        data = dict(data)
        if isinstance(data.get("status"), str):
            data["status"] = LeaseStatus(data["status"])
        return cls(**data)


@dataclass
class ResourcePool:
    """Capacity tracking pool for a resource type (spec §12)."""

    pool_id: str
    resource_type: ResourceType
    capacity: float
    allocated: float = 0.0
    reserved: float = 0.0
    available: float = 0.0
    health: str = "HEALTHY"
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.available == 0.0 and self.allocated == 0.0 and self.reserved == 0.0:
            self.available = self.capacity

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["resource_type"] = self.resource_type.value
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ResourcePool:
        data = dict(data)
        if isinstance(data.get("resource_type"), str):
            data["resource_type"] = ResourceType(data["resource_type"])
        return cls(**data)


@dataclass
class ResourceReservation:
    """Durable resource reservation per mission (spec §13)."""

    reservation_id: str
    mission_id: str
    agent_instance_id: str
    pool_id: str
    quantity: float
    created_at: float = field(default_factory=time.time)
    expires_at: float = field(default_factory=lambda: time.time() + 3600.0)
    status: ReservationStatus = ReservationStatus.RESERVED

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["status"] = self.status.value
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ResourceReservation:
        data = dict(data)
        if isinstance(data.get("status"), str):
            data["status"] = ReservationStatus(data["status"])
        return cls(**data)


@dataclass
class FleetQueueEntry:
    """Durable queue entry for deferred/pending admissions (spec §41, §42)."""

    queue_id: str
    mission_id: str
    principal_id: str
    requirements: dict[str, Any] = field(default_factory=dict)
    priority: int = 0
    enqueued_at: float = field(default_factory=time.time)
    defer_reason: str = ""
    attempt: int = 0
    generation: int = 1

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> FleetQueueEntry:
        return cls(**data)


@dataclass
class MissionMigration:
    """Durable mission migration record (spec §25)."""

    migration_id: str
    mission_id: str
    source_agent_instance_id: str
    target_agent_instance_id: str
    reason: str
    state_snapshot_ref: str
    started_at: float = field(default_factory=time.time)
    completed_at: float | None = None
    status: MigrationStatus = MigrationStatus.REQUESTED
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["status"] = self.status.value
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MissionMigration:
        data = dict(data)
        if isinstance(data.get("status"), str):
            data["status"] = MigrationStatus(data["status"])
        return cls(**data)


@dataclass
class CollaborationAssignment:
    """Multi-agent collaboration assignment per child goal (spec §31)."""

    collaboration_id: str
    mission_id: str
    goal_task_id: str
    agent_instance_id: str
    role: AgentRole
    scope: str = ""
    input_refs: list[str] = field(default_factory=list)
    expected_output: str = ""
    status: str = "ASSIGNED"
    result_evidence: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["role"] = self.role.value
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CollaborationAssignment:
        data = dict(data)
        if isinstance(data.get("role"), str):
            data["role"] = AgentRole(data["role"])
        return cls(**data)


class HandoffStatus(StrEnum):
    PROPOSED = "PROPOSED"
    ACCEPTED = "ACCEPTED"
    ACTIVE = "ACTIVE"
    RETURNED = "RETURNED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


@dataclass
class AgentHandoff:
    """Cross-agent handoff record using SessionEnvelope (spec §35).

    P1-02 spec fields: handoff_id, from_agent, to_agent, goal_scope,
    context_projection, capability_projection, ownership_transfer, return_policy, status.
    """

    handoff_id: str
    mission_id: str
    source_agent: str
    target_agent: str
    reason: str
    session_envelope_ref: str
    accepted_progress_ref: str
    workspace_ref: str
    created_at: float = field(default_factory=time.time)
    goal_scope: str = ""
    context_projection: str = ""
    capability_projection: list[str] = field(default_factory=list)
    ownership_transfer: bool = False
    return_policy: str = "on_completion"
    status: HandoffStatus = HandoffStatus.PROPOSED

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["status"] = self.status.value
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AgentHandoff:
        data = dict(data)
        if isinstance(data.get("status"), str):
            data["status"] = HandoffStatus(data["status"])
        return cls(**data)


@dataclass
class FleetHealth:
    """Fleet-wide health and capacity status (spec §46)."""

    fleet_status: str
    ready_agents: int
    busy_agents: int
    unavailable_agents: int
    queued_missions: int
    active_missions: int
    deferred_missions: int
    resource_saturation: float
    stale_leases: int
    failed_migrations: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> FleetHealth:
        return cls(**data)
