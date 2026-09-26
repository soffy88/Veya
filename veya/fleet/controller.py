"""Fleet Controller (spec §44, §46, §47, §68, §69).

Invariants:
- Sole coordination control plane for registry, placement, capacity, leases, migration, and lifecycle.
- Zero direct execution, zero semantic decision-making (FLEET_DIRECT_EXECUTION=0, FLEET_SEMANTIC_DECISION=0).
- Durable state recovery on crash restores queue, leases, and placements (FLEET_STATE_LOSS=0).
- Stale generations from old controllers are strictly denied (STALE_FLEET_COMMIT_ACCEPTED=0).
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

from .admission import FleetAdmissionController
from .collaboration import CollaborationManager
from .leases import LeaseManager
from .lifecycle import FleetLifecycleManager
from .migration import MigrationManager
from .models import (
    AgentInstance,
    AgentInstanceStatus,
    FleetHealth,
    FleetStatus,
    LeaseStatus,
    MigrationStatus,
    MissionMigration,
    PlacementDecision,
    PlacementRequest,
    SchedulingPolicy,
)
from .registry import FleetRegistry
from .resource import ResourceManager
from .scheduler import FleetScheduler


class FleetController:
    """Central fleet management plane orchestrating placement, capacity, and lifecycle."""

    def __init__(
        self,
        fleet_id: str = "default_fleet",
        base_dir: str | Path | None = None,
        policy: SchedulingPolicy = SchedulingPolicy.CAPABILITY_AWARE,
        global_max_missions: int = 100,
        per_principal_max_missions: int = 20,
        aging_interval_s: float = 30.0,
    ):
        self.fleet_id = fleet_id
        self._lock = threading.RLock()
        self.registry = FleetRegistry(fleet_id=fleet_id, base_dir=base_dir)

        # Subsystems
        self.admission = FleetAdmissionController(
            registry=self.registry,
            global_max_missions=global_max_missions,
            per_principal_max_missions=per_principal_max_missions,
        )
        self.scheduler = FleetScheduler(
            registry=self.registry,
            admission_controller=self.admission,
            policy=policy,
            aging_interval_s=aging_interval_s,
        )
        self.resources = ResourceManager(registry=self.registry)
        self.leases = LeaseManager(registry=self.registry)
        self.lifecycle = FleetLifecycleManager(
            registry=self.registry,
            lease_manager=self.leases,
        )
        self.migrations = MigrationManager(
            registry=self.registry,
            lease_manager=self.leases,
        )
        self.collaborations = CollaborationManager(registry=self.registry)

        # Recover durable state on init
        self.recover_state()

    def recover_state(self) -> None:
        """Durable state reconciliation on controller startup/restart (spec §42, §68)."""
        with self._lock:
            # 1. Sweep expired leases and reconcile resource reservations
            self.leases.sweep_expired_leases()
            self.resources.reconcile_expired()

            # 2. Check agent health
            self.lifecycle.detect_failures()

            # 3. Sweep completed draining agents
            self.lifecycle.sweep_draining_agents()

            # 4. If fleet was STARTING, promote to RUNNING
            fleet = self.registry.get_fleet()
            if fleet.status == FleetStatus.STARTING:
                self.registry.update_fleet_status(FleetStatus.RUNNING)

    def advance_generation(self) -> int:
        """Advance generation to fence out stale controllers (spec §11, §69)."""
        with self._lock:
            return self.registry.advance_fleet_generation()

    def register_agent(self, agent: AgentInstance) -> None:
        with self._lock:
            self.registry.register_agent(agent)

    def heartbeat_agent(
        self, agent_instance_id: str, generation: int | None = None
    ) -> AgentInstance:
        with self._lock:
            return self.lifecycle.record_heartbeat(agent_instance_id, generation=generation)

    def admit_and_schedule(
        self, request: PlacementRequest, auto_lease: bool = True
    ) -> tuple[PlacementDecision | None, str]:
        """Admit, schedule, and optionally acquire initial lease for mission."""
        with self._lock:
            decision, status = self.scheduler.schedule_mission(request)
            if decision is not None and auto_lease:
                existing_lease = self.registry.find_active_lease_for_mission(decision.mission_id)
                if existing_lease is None:
                    try:
                        self.leases.acquire_lease(
                            agent_instance_id=decision.agent_instance_id,
                            mission_id=decision.mission_id,
                        )
                    except Exception as e:
                        # Record failure and defer
                        decision.status = "FAILED"
                        self.registry.save_placement(decision)
                        return None, f"LEASE_FAILED: {e}"

            return decision, status

    def release_placement(self, mission_id: str) -> None:
        """Mark mission placement as released/completed and release leases/reservations."""
        with self._lock:
            plc = self.registry.find_active_placement_for_mission(mission_id)
            if plc:
                plc.status = "COMPLETED"
                self.registry.save_placement(plc)
            lease = self.registry.find_active_lease_for_mission(mission_id)
            if lease:
                self.leases.release_lease(lease.lease_id)
            self.resources.release_all_for_mission(mission_id)

    def process_queue(self) -> list[PlacementDecision]:
        """Process pending queue entries using anti-starvation policy."""
        with self._lock:
            placed: list[PlacementDecision] = []
            while True:
                decision, _entry = self.scheduler.schedule_next_queued()
                if decision is None:
                    break
                try:
                    self.leases.acquire_lease(
                        agent_instance_id=decision.agent_instance_id,
                        mission_id=decision.mission_id,
                    )
                    placed.append(decision)
                except Exception:
                    decision.status = "FAILED"
                    self.registry.save_placement(decision)
            return placed

    def drain_agent(self, agent_instance_id: str) -> None:
        with self._lock:
            self.lifecycle.drain_agent(agent_instance_id)

    def drain_fleet(self) -> None:
        with self._lock:
            self.lifecycle.drain_fleet()

    def recover_mission(self, mission_id: str) -> MissionMigration:
        """Find replacement placement and migrate orphaned mission (spec §24, §67)."""
        with self._lock:
            old_placement = self.registry.find_active_placement_for_mission(mission_id)
            req_dict = old_placement.manifest_snapshot if old_placement else {}
            req = (
                PlacementRequest.from_dict(req_dict)
                if req_dict
                else PlacementRequest(mission_id=mission_id, principal_id="system")
            )

            # Find capable replacement agent
            candidates = self.scheduler.find_candidates(req)
            if old_placement:
                # Exclude failing source agent
                candidates = [
                    c for c in candidates if c.agent_instance_id != old_placement.agent_instance_id
                ]

            if not candidates:
                raise RuntimeError(f"No capable replacement agent found for mission '{mission_id}'")

            target_agent = candidates[0]
            mig = self.migrations.initiate_migration(
                mission_id=mission_id,
                target_agent_instance_id=target_agent.agent_instance_id,
                reason="FAILURE_RECOVERY",
                state_snapshot_ref=f"snap_recovery_{mission_id}",
            )
            return self.migrations.execute_migration(mig.migration_id)

    def get_health(self) -> FleetHealth:
        """Compute fleet health and resource metrics (spec §46, §47)."""
        with self._lock:
            fleet = self.registry.get_fleet()
            agents = self.registry.list_agents()

            ready_count = sum(1 for a in agents if a.status == AgentInstanceStatus.READY)
            busy_count = sum(1 for a in agents if a.status == AgentInstanceStatus.BUSY)
            unavail_count = sum(
                1
                for a in agents
                if a.status in (AgentInstanceStatus.UNAVAILABLE, AgentInstanceStatus.STOPPED)
            )

            active_placements = [p for p in self.registry.list_placements() if p.status == "ACTIVE"]
            active_count = len(active_placements)
            queue_entries = self.registry.list_queue()
            queued_count = len(queue_entries)
            deferred_count = sum(1 for q in queue_entries if q.attempt > 0)

            saturation = self.resources.get_saturation()

            leases = self.registry.list_leases()
            now = time.time()
            stale_leases = sum(
                1
                for lease in leases
                if lease.status == LeaseStatus.ACTIVE and now > lease.expires_at
            )

            failed_migrations = sum(
                1 for m in self.registry.list_migrations() if m.status == MigrationStatus.FAILED
            )

            # Degraded detection
            if (
                unavail_count > 0
                and (ready_count > 0 or busy_count > 0)
                and fleet.status == FleetStatus.RUNNING
            ):
                fleet_status = FleetStatus.DEGRADED.value
            else:
                fleet_status = fleet.status.value

            return FleetHealth(
                fleet_status=fleet_status,
                ready_agents=ready_count,
                busy_agents=busy_count,
                unavailable_agents=unavail_count,
                queued_missions=queued_count,
                active_missions=active_count,
                deferred_missions=deferred_count,
                resource_saturation=saturation,
                stale_leases=stale_leases,
                failed_migrations=failed_migrations,
            )
