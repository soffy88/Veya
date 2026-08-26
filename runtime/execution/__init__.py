"""Durable execution primitives used by GoalRun and delegated workers.

This package deliberately contains deterministic execution concerns only.  It
does not choose tools, interpret user intent, or own the user-facing answer.
"""

from .artifacts import ArtifactStore
from .checkpoint import ExecutionCheckpointStore
from .delegate_runtime import DelegateRuntime
from .durable import (
    ClaimEnvelope,
    DurableExecutionError,
    DurableExecutionRepository,
    OutboxMessage,
    ReconciliationReport,
    WorkItemSpec,
    build_operation_key,
    canonical_json,
    content_hash,
    new_id,
)
from .fanin import FanInBatch, fan_in
from .finalization import (
    FinalizationController,
    FinalizationObserver,
    calculate_finalization_reserve,
)
from .models import (
    AcceptanceCriterion,
    AcceptanceResult,
    ArtifactManifest,
    ArtifactRef,
    Assertion,
    DelegateRequest,
    DelegateResult,
    DelegateStatus,
    Evidence,
    ExecutionCheckpoint,
    SharedTaskContext,
    SpawnBudget,
    StopReason,
)
from .outbox import OutboxPublisher
from .reconciler import Reconciler
from .runtime import DurableExecutionRuntime, DurableRuntimeConfig, get_durable_runtime
from .scheduler import ContinuousReadyScheduler, SchedulerRun
from .side_effects import SideEffectLedger
from .spawn_guard import SpawnGuard, SpawnRejected
from .worker import WorkerHost

__all__ = [
    "AcceptanceCriterion",
    "AcceptanceResult",
    "ArtifactManifest",
    "ArtifactRef",
    "ArtifactStore",
    "Assertion",
    "ClaimEnvelope",
    "ContinuousReadyScheduler",
    "DelegateRequest",
    "DelegateResult",
    "DelegateRuntime",
    "DelegateStatus",
    "DurableExecutionError",
    "DurableExecutionRepository",
    "DurableExecutionRuntime",
    "DurableRuntimeConfig",
    "Evidence",
    "ExecutionCheckpoint",
    "ExecutionCheckpointStore",
    "FanInBatch",
    "FinalizationController",
    "FinalizationObserver",
    "OutboxMessage",
    "OutboxPublisher",
    "Reconciler",
    "ReconciliationReport",
    "SchedulerRun",
    "SharedTaskContext",
    "SideEffectLedger",
    "SpawnBudget",
    "SpawnGuard",
    "SpawnRejected",
    "StopReason",
    "WorkItemSpec",
    "WorkerHost",
    "build_operation_key",
    "calculate_finalization_reserve",
    "canonical_json",
    "content_hash",
    "fan_in",
    "get_durable_runtime",
    "new_id",
]
