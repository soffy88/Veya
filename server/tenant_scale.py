"""P2-04 Multi-Tenant Scale Plane — tenant isolation, quotas, fair scheduling.

Only implement if Veya becomes a multi-user hosted platform.
Not required for current local-first convergence.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any


class TenantTier(StrEnum):
    FREE = "FREE"
    STANDARD = "STANDARD"
    ENTERPRISE = "ENTERPRISE"


class QuotaType(StrEnum):
    EXECUTIONS = "EXECUTIONS"
    TOKENS = "TOKENS"
    STORAGE = "STORAGE"
    AGENTS = "AGENTS"
    COMPUTE_HOURS = "COMPUTE_HOURS"


@dataclass(frozen=True)
class Quota:
    """A quota limit for a tenant."""

    quota_type: QuotaType
    limit: int
    used: int = 0
    reset_at: float | None = None

    @property
    def remaining(self) -> int:
        return max(0, self.limit - self.used)

    @property
    def exhausted(self) -> bool:
        return self.used >= self.limit


@dataclass
class Tenant:
    """A tenant in the multi-tenant scale plane."""

    tenant_id: str
    name: str
    tier: TenantTier = TenantTier.FREE
    quotas: dict[QuotaType, Quota] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "tenant_id": self.tenant_id,
            "name": self.name,
            "tier": self.tier.value,
            "quotas": {
                k.value: asdict(v) for k, v in self.quotas.items()
            },
            "created_at": self.created_at,
        }


@dataclass(frozen=True)
class FairShare:
    """Fair scheduling share for a tenant."""

    tenant_id: str
    weight: float = 1.0
    active_executions: int = 0
    max_concurrent: int = 10


def new_tenant(name: str, *, tier: TenantTier = TenantTier.FREE) -> Tenant:
    """Create a new tenant."""
    return Tenant(
        tenant_id=str(uuid.uuid4()),
        name=name,
        tier=tier,
    )


def check_quota(tenant: Tenant, quota_type: QuotaType, amount: int = 1) -> tuple[bool, str]:
    """Check if a tenant has sufficient quota."""
    quota = tenant.quotas.get(quota_type)
    if quota is None:
        return True, "no quota limit"
    if quota.exhausted:
        return False, f"quota exhausted: {quota_type.value}"
    if quota.remaining < amount:
        return False, f"insufficient quota: {quota_type.value} remaining={quota.remaining}"
    return True, "ok"


def consume_quota(tenant: Tenant, quota_type: QuotaType, amount: int = 1) -> bool:
    """Consume quota for a tenant. Returns True if successful."""
    quota = tenant.quotas.get(quota_type)
    if quota is None:
        return True
    if quota.remaining < amount:
        return False
    quota.used += amount
    return True


def fair_share_score(share: FairShare) -> float:
    """Compute fair scheduling score. Lower = higher priority."""
    if share.active_executions >= share.max_concurrent:
        return float("inf")
    return share.active_executions / share.weight
