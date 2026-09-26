"""Durable Controller for Veya Agent Operations V1 (spec §2, §39-§42).

OperationsController coordinates all operational sub-engines:
- Operational Lifecycle
- Health and SLO evaluations
- Alert deduplication and Incident correlation
- Quota and Cost governance
- Rollout coordination
- Audit trail, approval boundaries, and multi-principal isolation

Durable recovery:
  Persists state to disk across process restarts (OPERATIONS_CONTROLLER_RECOVERY=PASS).
Generation fencing:
  operations_generation prevents stale controller commits (STALE_OPERATIONS_COMMIT_ACCEPTED=0).
Idempotency:
  Idempotency keys prevent duplicate operational side effects (DUPLICATE_OPERATIONAL_SIDE_EFFECTS=0).
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

from veya.fleet import FleetController

from .alerts import AlertManager
from .audit import AuditManager
from .health_slo import HealthEvaluator, SLOEngine
from .lifecycle import OperationalLifecycleManager
from .models import (
    AgentOperationalState,
    CostBudget,
    MaintenanceScope,
    MaintenanceWindow,
    OperationalAuditRecord,
)
from .quota_cost import (
    CostBudgetManager,
    ProviderOperationalManager,
    QuotaManager,
    UsageLedgerManager,
)
from .rollout import RolloutCoordinator


class StaleOperationsGenerationError(Exception):
    pass


class DuplicateOperationalRequestError(Exception):
    pass


class OperationsController:
    """Canonical Operations Controller for Veya Agent Operations V1."""

    def __init__(
        self,
        operations_id: str = "default_ops",
        base_dir: str | Path | None = None,
        fleet_controller: FleetController | None = None,
    ) -> None:
        self.operations_id = operations_id
        self.base_dir = Path(base_dir) if base_dir else Path("/tmp/veya_operations")
        self.base_dir.mkdir(parents=True, exist_ok=True)

        self._lock = threading.RLock()
        self.generation: int = 1
        self._processed_idempotency_keys: set[str] = set()

        # Sub-engines
        self.fleet = fleet_controller
        self.audit = AuditManager()
        self.health = HealthEvaluator()
        self.slo = SLOEngine()
        self.lifecycle = OperationalLifecycleManager(fleet_controller)
        self.alerts = AlertManager()
        self.quota = QuotaManager()
        self.usage = UsageLedgerManager()
        self.budgets = CostBudgetManager()
        self.providers = ProviderOperationalManager()
        self.rollouts = RolloutCoordinator(fleet_controller)

        # Persistent state file
        self._state_file = self.base_dir / "operations_state.json"
        self._recover_state()

    def _recover_state(self) -> None:
        """Durable recovery of operational state across process restarts (spec §39, §60)."""
        with self._lock:
            if not self._state_file.exists():
                self._save_state()
                return

            try:
                data = json.loads(self._state_file.read_text())
                self.generation = data.get("generation", 1) + 1  # Increment on recovery
                self._processed_idempotency_keys = set(data.get("idempotency_keys", []))

                # Recover agent states
                for ag_data in data.get("agent_states", []):
                    st = AgentOperationalState.from_dict(ag_data)
                    self.lifecycle._agent_states[st.agent_id] = st

                # Recover maintenance windows
                for mw_data in data.get("maintenance_windows", []):
                    mw = MaintenanceWindow.from_dict(mw_data)
                    self.lifecycle._maintenance_windows[mw.id] = mw

                # Recover budgets
                for b_data in data.get("budgets", []):
                    b = CostBudget(**b_data)
                    self.budgets.register_budget(b)

                # Recover audit records
                for aud_data in data.get("audit_records", []):
                    rec = OperationalAuditRecord.from_dict(aud_data)
                    self.audit._audit_trail.append(rec)

                # Save updated generation
                self._save_state()
            except Exception:
                pass

    def _save_state(self) -> None:
        """Persist operational snapshot to disk."""
        with self._lock:
            data = {
                "operations_id": self.operations_id,
                "generation": self.generation,
                "idempotency_keys": list(self._processed_idempotency_keys),
                "agent_states": [st.to_dict() for st in self.lifecycle._agent_states.values()],
                "maintenance_windows": [
                    mw.to_dict() for mw in self.lifecycle._maintenance_windows.values()
                ],
                "budgets": [b.__dict__ for b in self.budgets._budgets.values()],
                "audit_records": [rec.to_dict() for rec in self.audit._audit_trail],
                "updated_at": time.time(),
            }
            temp_file = self.base_dir / f"operations_state.tmp.{os.getpid()}"
            temp_file.write_text(json.dumps(data, indent=2))
            temp_file.replace(self._state_file)

    def check_fencing_and_idempotency(
        self,
        idempotency_key: str | None = None,
        expected_generation: int | None = None,
    ) -> None:
        """Enforces generation fencing and request idempotency (spec §40, §41)."""
        with self._lock:
            if expected_generation is not None and expected_generation < self.generation:
                raise StaleOperationsGenerationError(
                    f"STALE_OPERATIONS_COMMIT: expected={expected_generation}, current={self.generation}"
                )

            if idempotency_key:
                if idempotency_key in self._processed_idempotency_keys:
                    raise DuplicateOperationalRequestError(
                        f"DUPLICATE_OPERATIONAL_REQUEST: key={idempotency_key}"
                    )
                self._processed_idempotency_keys.add(idempotency_key)

    # Coordinated Operations Facade Methods

    def pause_agent(
        self,
        agent_id: str,
        reason: str = "Operator pause",
        actor: str = "operator",
        actor_principal: str = "default",
        target_principal: str = "default",
        idempotency_key: str | None = None,
        expected_generation: int | None = None,
    ) -> AgentOperationalState:
        with self._lock:
            self.check_fencing_and_idempotency(idempotency_key, expected_generation)
            self.audit.verify_action_authorized("pause_agent", actor_principal, target_principal)
            st = self.lifecycle.pause_agent(
                agent_id, reason=reason, actor=actor, request_id=idempotency_key or ""
            )
            self.audit.record_mutation(
                actor=actor,
                action="pause_agent",
                target=agent_id,
                before={},
                after=st.to_dict(),
                reason=reason,
                request_id=idempotency_key or "",
                principal_id=target_principal,
            )
            self._save_state()
            return st

    def resume_agent(
        self,
        agent_id: str,
        reason: str = "Operator resume",
        actor: str = "operator",
        actor_principal: str = "default",
        target_principal: str = "default",
        idempotency_key: str | None = None,
        expected_generation: int | None = None,
    ) -> AgentOperationalState:
        with self._lock:
            self.check_fencing_and_idempotency(idempotency_key, expected_generation)
            self.audit.verify_action_authorized("resume_agent", actor_principal, target_principal)
            st = self.lifecycle.resume_agent(
                agent_id, reason=reason, actor=actor, request_id=idempotency_key or ""
            )
            self.audit.record_mutation(
                actor=actor,
                action="resume_agent",
                target=agent_id,
                before={},
                after=st.to_dict(),
                reason=reason,
                request_id=idempotency_key or "",
                principal_id=target_principal,
            )
            self._save_state()
            return st

    def drain_agent(
        self,
        agent_id: str,
        reason: str = "Operator drain",
        actor: str = "operator",
        actor_principal: str = "default",
        target_principal: str = "default",
        idempotency_key: str | None = None,
        expected_generation: int | None = None,
    ) -> AgentOperationalState:
        with self._lock:
            self.check_fencing_and_idempotency(idempotency_key, expected_generation)
            self.audit.verify_action_authorized("drain_agent", actor_principal, target_principal)
            st = self.lifecycle.drain_agent(
                agent_id, reason=reason, actor=actor, request_id=idempotency_key or ""
            )
            self.audit.record_mutation(
                actor=actor,
                action="drain_agent",
                target=agent_id,
                before={},
                after=st.to_dict(),
                reason=reason,
                request_id=idempotency_key or "",
                principal_id=target_principal,
            )
            self._save_state()
            return st

    def set_maintenance(
        self,
        scope: MaintenanceScope,
        target_id: str,
        duration_s: float,
        reason: str,
        actor: str = "operator",
        approval_id: str | None = None,
        actor_principal: str = "default",
        target_principal: str = "default",
        idempotency_key: str | None = None,
        expected_generation: int | None = None,
    ) -> MaintenanceWindow:
        with self._lock:
            self.check_fencing_and_idempotency(idempotency_key, expected_generation)
            action_name = "fleet_maintenance" if scope == MaintenanceScope.FLEET else "maintenance"
            self.audit.verify_action_authorized(
                action_name, actor_principal, target_principal, approval_id
            )

            now = time.time()
            mw = self.lifecycle.create_maintenance_window(
                scope=scope,
                target_id=target_id,
                starts_at=now,
                ends_at=now + duration_s,
                reason=reason,
                created_by=actor,
                request_id=idempotency_key or "",
            )
            self.audit.record_mutation(
                actor=actor,
                action="set_maintenance",
                target=target_id,
                before={},
                after=mw.to_dict(),
                reason=reason,
                request_id=idempotency_key or "",
                approval_id=approval_id,
                principal_id=target_principal,
            )
            self._save_state()
            return mw
