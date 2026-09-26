"""veya.fleet: Agent Fleet Management and Coordination Layer (spec §0-§93).

Invariants:
- MasterAgent is the sole semantic authority within a Mission.
- GoalRun is the durable execution authority.
- Fleet is placement / capacity / isolation / coordination authority only.
"""

from __future__ import annotations

from .admission import AdmissionDecision, FleetAdmissionController
from .collaboration import CollaborationAuthorityError, CollaborationManager
from .controller import FleetController
from .events import FleetEvent, FleetEventEmitter
from .leases import LeaseConflictError, LeaseManager
from .lifecycle import FleetLifecycleManager
from .migration import MigrationError, MigrationManager
from .models import (
    AdmissionOutcome,
    AgentFleet,
    AgentHandoff,
    AgentInstance,
    AgentInstanceStatus,
    AgentLease,
    AgentRole,
    CollaborationAssignment,
    FleetHealth,
    FleetQueueEntry,
    FleetStatus,
    LeaseStatus,
    MigrationStatus,
    MissionMigration,
    PlacementDecision,
    PlacementRequest,
    ReservationStatus,
    ResourcePool,
    ResourceReservation,
    ResourceType,
    SchedulingPolicy,
)
from .observability import FleetMetrics, MetricSummary, calculate_percentile
from .registry import FleetRegistry, StaleGenerationError
from .resource import ResourceExhaustedError, ResourceManager
from .scheduler import FleetScheduler

__all__ = [
    "AdmissionDecision",
    "AdmissionOutcome",
    "AgentFleet",
    "AgentHandoff",
    "AgentInstance",
    "AgentInstanceStatus",
    "AgentLease",
    "AgentRole",
    "CollaborationAssignment",
    "CollaborationAuthorityError",
    "CollaborationManager",
    "FleetAdmissionController",
    "FleetController",
    "FleetEvent",
    "FleetEventEmitter",
    "FleetHealth",
    "FleetLifecycleManager",
    "FleetMetrics",
    "FleetQueueEntry",
    "FleetRegistry",
    "FleetScheduler",
    "FleetStatus",
    "LeaseConflictError",
    "LeaseManager",
    "LeaseStatus",
    "MetricSummary",
    "MigrationError",
    "MigrationManager",
    "MigrationStatus",
    "MissionMigration",
    "PlacementDecision",
    "PlacementRequest",
    "ReservationStatus",
    "ResourceExhaustedError",
    "ResourceManager",
    "ResourcePool",
    "ResourceReservation",
    "ResourceType",
    "SchedulingPolicy",
    "StaleGenerationError",
    "calculate_percentile",
]
