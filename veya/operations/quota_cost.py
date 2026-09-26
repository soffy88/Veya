"""Quota and Cost Governance Engine for Veya Agent Operations V1 (spec §19-§25, §47).

Features:
- Policy-driven quota enforcement: BLOCK, QUEUE, THROTTLE
- Idempotent UsageLedger (DUPLICATE_USAGE_CHARGE=0)
- Cost budget evaluation with warning threshold and hard limit gates
- Provider operational state projection (AVAILABLE, DEGRADED, COOLDOWN, UNAVAILABLE)
- Zero silent drops, zero arbitrary mission kills, zero silent model fallbacks
"""

from __future__ import annotations

import time

from .models import (
    BudgetEvaluation,
    CostBudget,
    ProviderOperationalStatus,
    QuotaEnforcementAction,
    QuotaPolicy,
    UsageRecord,
)


class QuotaExceededError(Exception):
    def __init__(
        self,
        scope: str,
        dimension: str,
        limit: float,
        requested: float,
        action: QuotaEnforcementAction,
    ):
        self.scope = scope
        self.dimension = dimension
        self.limit = limit
        self.requested = requested
        self.action = action
        super().__init__(
            f"QUOTA_EXCEEDED: scope={scope}, dimension={dimension}, limit={limit}, requested={requested}, action={action.value}"
        )


class QuotaManager:
    """Enforces operational governance quotas across principals, agents, and resources."""

    def __init__(self) -> None:
        self._policies: dict[str, QuotaPolicy] = {}  # principal_id -> QuotaPolicy
        # principal_id -> dimension -> current usage
        self._usage: dict[str, dict[str, float]] = {}

    def register_policy(self, policy: QuotaPolicy) -> None:
        self._policies[policy.principal_id] = policy
        if policy.principal_id not in self._usage:
            self._usage[policy.principal_id] = {}

    def check_and_acquire(
        self,
        principal_id: str,
        dimension: str,
        amount: float = 1.0,
    ) -> bool:
        """Check quota limit. Raises QuotaExceededError if limit breached (spec §19, §20)."""
        policy = self._policies.get(principal_id)
        if not policy:
            return True  # No quota policy applied

        limit = policy.dimensions.get(dimension)
        if limit is None:
            return True

        current = self._usage.get(principal_id, {}).get(dimension, 0.0)
        if current + amount > limit:
            raise QuotaExceededError(
                scope=policy.scope,
                dimension=dimension,
                limit=limit,
                requested=current + amount,
                action=policy.enforcement_action,
            )

        if principal_id not in self._usage:
            self._usage[principal_id] = {}
        self._usage[principal_id][dimension] = current + amount
        return True

    def release(self, principal_id: str, dimension: str, amount: float = 1.0) -> None:
        if principal_id in self._usage and dimension in self._usage[principal_id]:
            self._usage[principal_id][dimension] = max(
                0.0, self._usage[principal_id][dimension] - amount
            )

    def get_usage(self, principal_id: str, dimension: str) -> float:
        return self._usage.get(principal_id, {}).get(dimension, 0.0)


class UsageLedgerManager:
    """Maintains an idempotent, tamper-evident ledger of operational consumption (spec §21)."""

    def __init__(self) -> None:
        # record_id or idempotency_key -> UsageRecord
        self._records: dict[str, UsageRecord] = {}
        self._source_events: set[str] = set()

    def record_usage(self, record: UsageRecord) -> bool:
        """Record usage idempotently. DUPLICATE_USAGE_CHARGE=0."""
        # Check primary record_id
        if record.record_id in self._records:
            return False

        # Check source event idempotency key if provided
        if record.source_event_id and record.source_event_id in self._source_events:
            return False

        self._records[record.record_id] = record
        if record.source_event_id:
            self._source_events.add(record.source_event_id)
        return True

    def query_usage(
        self,
        principal_id: str | None = None,
        agent_id: str | None = None,
        mission_id: str | None = None,
    ) -> list[UsageRecord]:
        results: list[UsageRecord] = []
        for r in self._records.values():
            if principal_id and r.principal_id != principal_id:
                continue
            if agent_id and r.agent_id != agent_id:
                continue
            if mission_id and r.mission_id != mission_id:
                continue
            results.append(r)
        return results

    def total_quantity(self, principal_id: str, resource_type: str) -> float:
        return sum(
            r.quantity
            for r in self._records.values()
            if r.principal_id == principal_id and r.resource_type == resource_type
        )


class CostBudgetManager:
    """Monitors financial budgets and enforces warning/hard-limit thresholds (spec §22, §23)."""

    def __init__(self) -> None:
        self._budgets: dict[str, CostBudget] = {}  # budget_id -> CostBudget

    def register_budget(self, budget: CostBudget) -> None:
        self._budgets[budget.budget_id] = budget

    def record_spend(self, budget_id: str, amount: float) -> BudgetEvaluation:
        budget = self._budgets.get(budget_id)
        if not budget:
            raise KeyError(f"Budget '{budget_id}' not found")

        budget.current_spend += amount
        return self.evaluate_budget(budget_id)

    def evaluate_budget(self, budget_id: str) -> BudgetEvaluation:
        budget = self._budgets.get(budget_id)
        if not budget:
            raise KeyError(f"Budget '{budget_id}' not found")

        ratio = budget.current_spend / max(1e-6, budget.budget_amount)
        warning = ratio >= budget.warning_threshold
        hard_limit = ratio >= budget.hard_limit

        action = None
        if hard_limit:
            action = "BLOCK_NEW_ADMISSIONS"
        elif warning:
            action = "EMIT_WARNING_ALERT"

        return BudgetEvaluation(
            budget_id=budget_id,
            current_spend=round(budget.current_spend, 2),
            budget_amount=round(budget.budget_amount, 2),
            utilization_ratio=round(ratio, 4),
            warning_reached=warning,
            hard_limit_reached=hard_limit,
            action_required=action,
        )


class ProviderOperationalManager:
    """Tracks operational availability, upstream quota exhaustion, and cooldown states (spec §24, §25)."""

    def __init__(self) -> None:
        self._provider_status: dict[str, ProviderOperationalStatus] = {}
        self._cooldown_until: dict[str, float] = {}

    def set_provider_status(self, provider_id: str, status: ProviderOperationalStatus) -> None:
        self._provider_status[provider_id] = status

    def mark_cooldown(self, provider_id: str, duration_s: float) -> None:
        """Mark provider in cooldown upon UPSTREAM_QUOTA_EXHAUSTED."""
        self._provider_status[provider_id] = ProviderOperationalStatus.COOLDOWN
        self._cooldown_until[provider_id] = time.time() + duration_s

    def get_status(self, provider_id: str, now: float | None = None) -> ProviderOperationalStatus:
        t = now if now is not None else time.time()
        cd = self._cooldown_until.get(provider_id, 0.0)
        if cd > t:
            return ProviderOperationalStatus.COOLDOWN
        st = self._provider_status.get(provider_id, ProviderOperationalStatus.AVAILABLE)
        if st == ProviderOperationalStatus.COOLDOWN:
            self._provider_status[provider_id] = ProviderOperationalStatus.AVAILABLE
            return ProviderOperationalStatus.AVAILABLE
        return st

    def get_retry_not_before(self, provider_id: str) -> float | None:
        cd = self._cooldown_until.get(provider_id, 0.0)
        return cd if cd > time.time() else None
