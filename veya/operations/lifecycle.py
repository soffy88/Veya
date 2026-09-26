"""Operational Lifecycle Management for Veya Agent Operations V1 (spec §4-§7, §45).

Manages desired vs observed operational states:
  ACTIVE, DRAINING, PAUSED, MAINTENANCE, DEGRADED, BLOCKED, DISABLED
Coordinates transitions:
  enable, disable, pause, resume, drain, maintenance, retire.
Integrates with Fleet and AgentRuntime exclusively through canonical APIs:
  Zero direct process kill, zero direct fleet store write, zero mission cancellation.
"""

from __future__ import annotations

import contextlib
import time
from typing import Any

from veya.fleet import AgentInstanceStatus, FleetController

from .models import (
    AgentOperationalState,
    AgentOperationalStatus,
    MaintenanceScope,
    MaintenanceWindow,
    OperationalAuditRecord,
)


class OperationalLifecycleManager:
    """Coordinates agent operational states, maintenance windows, and convergence."""

    def __init__(self, fleet_controller: FleetController | None = None) -> None:
        self.fleet = fleet_controller
        self._agent_states: dict[str, AgentOperationalState] = {}
        self._maintenance_windows: dict[str, MaintenanceWindow] = {}
        self._audit_records: list[OperationalAuditRecord] = []

    def set_fleet_controller(self, fleet: FleetController) -> None:
        self.fleet = fleet

    def get_agent_state(self, agent_id: str) -> AgentOperationalState:
        if agent_id not in self._agent_states:
            self._agent_states[agent_id] = AgentOperationalState(
                agent_id=agent_id,
                desired_state=AgentOperationalStatus.ACTIVE,
                observed_state=AgentOperationalStatus.ACTIVE,
            )
        return self._agent_states[agent_id]

    def pause_agent(
        self,
        agent_id: str,
        reason: str = "Operator requested pause",
        actor: str = "operator",
        request_id: str = "",
    ) -> AgentOperationalState:
        """Pause agent: do not admit new operational work without terminating active missions (spec §4, §5)."""
        st = self.get_agent_state(agent_id)
        before = st.to_dict()
        st.desired_state = AgentOperationalStatus.PAUSED
        st.reason = reason
        st.updated_at = time.time()

        # Reconcile with Fleet: update fleet status to WAITING if agent exists to prevent admission
        if self.fleet:
            with contextlib.suppress(Exception):
                # Fleet status WAITING prevents scheduler admissions
                self.fleet.registry.update_agent_status(agent_id, AgentInstanceStatus.WAITING)
        st.observed_state = AgentOperationalStatus.PAUSED

        self._record_audit(
            actor=actor,
            action="pause_agent",
            target=agent_id,
            before=before,
            after=st.to_dict(),
            reason=reason,
            request_id=request_id,
        )
        return st

    def resume_agent(
        self,
        agent_id: str,
        reason: str = "Operator requested resume",
        actor: str = "operator",
        request_id: str = "",
    ) -> AgentOperationalState:
        """Resume agent: restore to ACTIVE status allowing admissions."""
        st = self.get_agent_state(agent_id)
        before = st.to_dict()
        st.desired_state = AgentOperationalStatus.ACTIVE
        st.reason = reason
        st.updated_at = time.time()

        if self.fleet:
            with contextlib.suppress(Exception):
                self.fleet.registry.update_agent_status(agent_id, AgentInstanceStatus.READY)
        st.observed_state = AgentOperationalStatus.ACTIVE

        self._record_audit(
            actor=actor,
            action="resume_agent",
            target=agent_id,
            before=before,
            after=st.to_dict(),
            reason=reason,
            request_id=request_id,
        )
        return st

    def drain_agent(
        self,
        agent_id: str,
        reason: str = "Operator requested drain",
        actor: str = "operator",
        request_id: str = "",
    ) -> AgentOperationalState:
        """Drain agent: stop new admissions and allow active work to complete/migrate (spec §6, §51)."""
        st = self.get_agent_state(agent_id)
        before = st.to_dict()
        st.desired_state = AgentOperationalStatus.DRAINING
        st.reason = reason
        st.updated_at = time.time()

        if self.fleet:
            with contextlib.suppress(Exception):
                # Calls canonical Fleet drain API
                self.fleet.drain_agent(agent_id)
        st.observed_state = AgentOperationalStatus.DRAINING

        self._record_audit(
            actor=actor,
            action="drain_agent",
            target=agent_id,
            before=before,
            after=st.to_dict(),
            reason=reason,
            request_id=request_id,
        )
        return st

    def disable_agent(
        self,
        agent_id: str,
        reason: str = "Operator requested disable",
        actor: str = "operator",
        request_id: str = "",
    ) -> AgentOperationalState:
        """Disable agent: DISABLE != DELETE (spec §6). Preserves registration and audit history."""
        st = self.get_agent_state(agent_id)
        before = st.to_dict()
        st.desired_state = AgentOperationalStatus.DISABLED
        st.reason = reason
        st.updated_at = time.time()

        if self.fleet:
            with contextlib.suppress(Exception):
                self.fleet.registry.update_agent_status(agent_id, AgentInstanceStatus.UNAVAILABLE)
        st.observed_state = AgentOperationalStatus.DISABLED

        self._record_audit(
            actor=actor,
            action="disable_agent",
            target=agent_id,
            before=before,
            after=st.to_dict(),
            reason=reason,
            request_id=request_id,
        )
        return st

    def retire_agent(
        self,
        agent_id: str,
        reason: str = "Agent decommissioned",
        actor: str = "operator",
        request_id: str = "",
    ) -> AgentOperationalState:
        """Retire agent: stop admissions, drain active work, retain audit history (spec §6)."""
        self.drain_agent(agent_id, reason=reason, actor=actor, request_id=request_id)
        st = self.get_agent_state(agent_id)
        before = st.to_dict()
        st.desired_state = AgentOperationalStatus.DISABLED
        st.observed_state = AgentOperationalStatus.DISABLED
        st.reason = f"RETIRED: {reason}"
        st.updated_at = time.time()

        self._record_audit(
            actor=actor,
            action="retire_agent",
            target=agent_id,
            before=before,
            after=st.to_dict(),
            reason=reason,
            request_id=request_id,
        )
        return st

    def create_maintenance_window(
        self,
        scope: MaintenanceScope,
        target_id: str,
        starts_at: float,
        ends_at: float,
        reason: str,
        created_by: str = "operator",
        policy: dict[str, Any] | None = None,
        request_id: str = "",
    ) -> MaintenanceWindow:
        """Create an operational maintenance window (spec §7)."""
        window_id = f"mw_{target_id}_{int(starts_at)}"
        mw = MaintenanceWindow(
            id=window_id,
            scope=scope,
            target_id=target_id,
            starts_at=starts_at,
            ends_at=ends_at,
            reason=reason,
            created_by=created_by,
            policy=policy or {},
            status="ACTIVE",
        )
        self._maintenance_windows[window_id] = mw

        if scope == MaintenanceScope.AGENT and mw.is_active():
            st = self.get_agent_state(target_id)
            st.observed_state = AgentOperationalStatus.MAINTENANCE
            if self.fleet:
                with contextlib.suppress(Exception):
                    self.fleet.registry.update_agent_status(target_id, AgentInstanceStatus.WAITING)

        self._record_audit(
            actor=created_by,
            action="create_maintenance_window",
            target=target_id,
            before={},
            after=mw.to_dict(),
            reason=reason,
            request_id=request_id,
        )
        return mw

    def is_target_in_maintenance(
        self, scope: MaintenanceScope, target_id: str, now: float | None = None
    ) -> bool:
        t = now if now is not None else time.time()
        for mw in self._maintenance_windows.values():
            if (
                mw.scope == scope
                and (mw.target_id == target_id or mw.target_id == "all")
                and mw.is_active(t)
            ):
                return True
        return False

    def list_audit_records(self) -> list[OperationalAuditRecord]:
        return list(self._audit_records)

    def _record_audit(
        self,
        actor: str,
        action: str,
        target: str,
        before: dict[str, Any],
        after: dict[str, Any],
        reason: str,
        request_id: str = "",
    ) -> None:
        rec = OperationalAuditRecord(
            actor=actor,
            action=action,
            target=target,
            before=before,
            after=after,
            reason=reason,
            timestamp=time.time(),
            request_id=request_id,
        )
        self._audit_records.append(rec)
