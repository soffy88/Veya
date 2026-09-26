"""Veya Agent Operations V1 Package.

Operational Governance plane for multi-agent systems:
- Lifecycle management (pause, resume, drain, disable, maintenance)
- Rolling rollout and rollback with runtime generation fencing
- Health policies and SLO evaluations
- Alert deduplication and incident correlation
- Quotas, idempotent usage ledger, and cost budgets
- Audit trail with approval barriers and multi-principal isolation
"""

from __future__ import annotations

from .alerts import AlertManager
from .audit import (
    AuditManager,
    CrossPrincipalAccessDeniedError,
    OperationalApprovalRequiredError,
)
from .controller import (
    DuplicateOperationalRequestError,
    OperationsController,
    StaleOperationsGenerationError,
)
from .health_slo import HealthEvaluator, SLOEngine
from .lifecycle import OperationalLifecycleManager
from .models import (
    AgentOperationalState,
    AgentOperationalStatus,
    AlertInstance,
    AlertRule,
    AlertState,
    BudgetEvaluation,
    CostBudget,
    FleetOperationalState,
    HealthPolicy,
    HealthState,
    IncidentStatus,
    MaintenanceScope,
    MaintenanceWindow,
    OperationalAuditRecord,
    OperationalIncident,
    OperationalPolicy,
    ProviderOperationalStatus,
    QuotaEnforcementAction,
    QuotaPolicy,
    RolloutPlan,
    RolloutRevision,
    RolloutStatus,
    RolloutTarget,
    SLODefinition,
    SLOEvaluation,
    SLOWindow,
    UsageRecord,
)
from .quota_cost import (
    CostBudgetManager,
    ProviderOperationalManager,
    QuotaExceededError,
    QuotaManager,
    UsageLedgerManager,
)
from .rollout import RolloutCoordinator, StaleGenerationCommitError

__all__ = [
    "AgentOperationalState",
    "AgentOperationalStatus",
    "AlertInstance",
    "AlertManager",
    "AlertRule",
    "AlertState",
    "AuditManager",
    "BudgetEvaluation",
    "CostBudget",
    "CostBudgetManager",
    "CrossPrincipalAccessDeniedError",
    "DuplicateOperationalRequestError",
    "FleetOperationalState",
    "HealthEvaluator",
    "HealthPolicy",
    "HealthState",
    "IncidentStatus",
    "MaintenanceScope",
    "MaintenanceWindow",
    "OperationalApprovalRequiredError",
    "OperationalAuditRecord",
    "OperationalIncident",
    "OperationalLifecycleManager",
    "OperationalPolicy",
    "OperationsController",
    "ProviderOperationalManager",
    "ProviderOperationalStatus",
    "QuotaEnforcementAction",
    "QuotaExceededError",
    "QuotaManager",
    "QuotaPolicy",
    "RolloutCoordinator",
    "RolloutPlan",
    "RolloutRevision",
    "RolloutStatus",
    "RolloutTarget",
    "SLODefinition",
    "SLOEngine",
    "SLOEvaluation",
    "SLOWindow",
    "StaleGenerationCommitError",
    "StaleOperationsGenerationError",
    "UsageLedgerManager",
    "UsageRecord",
]
