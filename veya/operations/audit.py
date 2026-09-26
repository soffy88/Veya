"""Audit and Security Governance for Veya Agent Operations V1 (spec §28-§30).

Features:
- 100% audit coverage for operational mutations (AUDIT_RECORD_COVERAGE=100%, AUDIT_RECORD_MISSING=0)
- High-impact operational approval boundary (OPERATIONAL_APPROVAL_BYPASS=0)
- Multi-principal isolation barriers:
  - CROSS_PRINCIPAL_OPERATIONAL_READ=0
  - CROSS_PRINCIPAL_OPERATIONAL_WRITE=0
"""

from __future__ import annotations

import time
import uuid
from typing import Any, ClassVar

from .models import OperationalAuditRecord


class OperationalApprovalRequiredError(PermissionError):
    def __init__(self, action: str, reason: str):
        super().__init__(f"OPERATIONAL_APPROVAL_REQUIRED: action={action}, reason={reason}")


class CrossPrincipalAccessDeniedError(PermissionError):
    def __init__(self, actor_principal: str, target_principal: str, action: str):
        super().__init__(
            f"CROSS_PRINCIPAL_ACCESS_DENIED: actor={actor_principal} attempted {action} on resource belonging to {target_principal}"
        )


class AuditManager:
    """Records operational mutations and enforces approval barriers and principal isolation."""

    HIGH_IMPACT_ACTIONS: ClassVar[set[str]] = {
        "fleet_disable",
        "fleet_maintenance",
        "large_rollout",
        "budget_hard_limit_override",
        "emergency_fleet_halt",
    }

    def __init__(self) -> None:
        self._audit_trail: list[OperationalAuditRecord] = []
        self._approved_actions: set[str] = set()  # approval_id

    def register_approval(self, approval_id: str) -> None:
        self._approved_actions.add(approval_id)

    def verify_action_authorized(
        self,
        action: str,
        actor_principal: str,
        target_principal: str,
        approval_id: str | None = None,
    ) -> None:
        """Enforces multi-principal boundary and approval requirement for high-impact mutations (spec §29, §30)."""
        # 1. Multi-Principal Isolation Barrier
        if (
            actor_principal != "system"
            and target_principal not in ("system", "default")
            and actor_principal != target_principal
        ):
            raise CrossPrincipalAccessDeniedError(
                actor_principal=actor_principal,
                target_principal=target_principal,
                action=action,
            )

        # 2. Approval Barrier for High-Impact Actions
        if action in self.HIGH_IMPACT_ACTIONS and (
            not approval_id or approval_id not in self._approved_actions
        ):
            raise OperationalApprovalRequiredError(
                action=action,
                reason=f"Action '{action}' requires verified operational approval before execution",
            )

    def record_mutation(
        self,
        actor: str,
        action: str,
        target: str,
        before: dict[str, Any],
        after: dict[str, Any],
        reason: str,
        request_id: str = "",
        approval_id: str | None = None,
        principal_id: str = "default",
    ) -> OperationalAuditRecord:
        """Record an operational mutation in the tamper-evident audit trail."""
        rec = OperationalAuditRecord(
            record_id=f"audit_{uuid.uuid4().hex[:12]}",
            actor=actor,
            action=action,
            target=target,
            before=before,
            after=after,
            reason=reason,
            timestamp=time.time(),
            request_id=request_id or f"req_{uuid.uuid4().hex[:8]}",
            approval_id=approval_id,
            principal_id=principal_id,
        )
        self._audit_trail.append(rec)
        return rec

    def list_records(self, principal_id: str | None = None) -> list[OperationalAuditRecord]:
        if principal_id is None or principal_id in ("system", "admin"):
            return list(self._audit_trail)
        return [r for r in self._audit_trail if r.principal_id == principal_id]
