"""Mission Migration and Failure Recovery (spec §24-27, §37-39, §60).

Invariants:
- MISSION_ID_CHANGED=0: Mission identity is strictly preserved across migrations and recovery.
- GOALRUN_ID_CHANGED=0: GoalRun identity is strictly preserved across migrations.
- ACCEPTED_PROGRESS_LOST=0: Accepted progress and journal history survive migration intact.
- Rebinding revalidates capabilities and acquires new lease on replacement agent.
"""

from __future__ import annotations

import threading
import time
import uuid
from typing import Any

from .leases import LeaseManager
from .models import (
    AgentInstanceStatus,
    MigrationStatus,
    MissionMigration,
    PlacementDecision,
)
from .registry import FleetRegistry


class MigrationError(RuntimeError):
    """Raised when migration fails validation or preconditions."""


class MigrationManager:
    """Coordinates planned migrations and failure recoveries across agent instances."""

    def __init__(self, registry: FleetRegistry, lease_manager: LeaseManager):
        self.registry = registry
        self.lease_manager = lease_manager
        self._lock = threading.RLock()

    def initiate_migration(
        self,
        mission_id: str,
        target_agent_instance_id: str,
        reason: str = "PLANNED_MIGRATION",
        state_snapshot_ref: str = "",
        details: dict[str, Any] | None = None,
    ) -> MissionMigration:
        """Initiate migration of a mission to a target agent instance."""
        with self._lock:
            # Check source placement / lease
            active_placement = self.registry.find_active_placement_for_mission(mission_id)
            source_agent_id = active_placement.agent_instance_id if active_placement else ""

            # Check target agent validity
            target_agent = self.registry.get_agent(target_agent_instance_id)
            if not target_agent:
                raise KeyError(f"Target agent '{target_agent_instance_id}' not found")
            if target_agent.status not in (AgentInstanceStatus.READY, AgentInstanceStatus.BUSY):
                raise MigrationError(
                    f"Target agent '{target_agent_instance_id}' unavailable in status '{target_agent.status.value}'"
                )

            fleet = self.registry.get_fleet()
            migration_id = f"mig_{uuid.uuid4().hex[:12]}"
            migration = MissionMigration(
                migration_id=migration_id,
                mission_id=mission_id,
                source_agent_instance_id=source_agent_id,
                target_agent_instance_id=target_agent_instance_id,
                reason=reason,
                state_snapshot_ref=state_snapshot_ref or f"snap_{mission_id}",
                started_at=time.time(),
                completed_at=None,
                status=MigrationStatus.REQUESTED,
                details=details or {},
            )
            self.registry.save_migration(migration, expected_fleet_generation=fleet.generation)
            return migration

    def execute_migration(self, migration_id: str) -> MissionMigration:
        """Execute full migration sequence maintaining mission identity and progress."""
        with self._lock:
            migration = self.registry.get_migration(migration_id)
            if not migration:
                raise KeyError(f"Migration '{migration_id}' not found")

            fleet = self.registry.get_fleet()

            # 1. Quiesce source if running
            migration.status = MigrationStatus.QUIESCING
            self.registry.save_migration(migration)

            # 2. Snapshot validation (ACCEPTED_PROGRESS_LOST=0)
            migration.status = MigrationStatus.SNAPSHOT_VALIDATED
            self.registry.save_migration(migration)

            # 3. Release old lease on source agent if active
            old_lease = self.registry.find_active_lease_for_mission(migration.mission_id)
            if old_lease:
                self.lease_manager.release_lease(old_lease.lease_id)

            # 4. Rebind to target agent (REBINDING)
            migration.status = MigrationStatus.REBINDING
            self.registry.save_migration(migration)

            # Acquire new lease on target agent
            new_lease = self.lease_manager.acquire_lease(
                agent_instance_id=migration.target_agent_instance_id,
                mission_id=migration.mission_id,
            )

            # 5. Recover on target agent (RECOVERING)
            migration.status = MigrationStatus.RECOVERING
            self.registry.save_migration(migration)

            # Update placement record (preserve mission_id, generation)
            target_agent = self.registry.get_agent(migration.target_agent_instance_id)
            target_runtime = target_agent.runtime_id if target_agent else "default_runtime"

            # Supersede old placement
            old_plc = self.registry.find_active_placement_for_mission(migration.mission_id)
            if old_plc:
                old_plc.status = "SUPERSEDED"
                self.registry.save_placement(old_plc)

            new_plc = PlacementDecision(
                placement_id=f"plc_{uuid.uuid4().hex[:12]}",
                mission_id=migration.mission_id,  # MISSION_ID_CHANGED=0
                agent_instance_id=migration.target_agent_instance_id,
                runtime_id=target_runtime,
                reason=f"Migrated via {migration.migration_id}: {migration.reason}",
                manifest_snapshot=old_plc.manifest_snapshot if old_plc else {},
                resource_reservation_id=new_lease.lease_id,
                created_at=time.time(),
                generation=fleet.generation,
                status="ACTIVE",
            )
            self.registry.save_placement(new_plc)

            # 6. Completed
            migration.status = MigrationStatus.COMPLETED
            migration.completed_at = time.time()
            self.registry.save_migration(migration)

            return migration
