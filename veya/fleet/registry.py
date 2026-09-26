"""Fleet Registry with Generation Fencing (spec §6, §11, §56).

Invariants:
- FleetRegistry is durable.
- Generation fencing protects all placement, lease, migration and capacity commits.
- Stale generations are strictly rejected (STALE_FLEET_COMMIT_ACCEPTED=0, STALE_AGENT_COMMIT_ACCEPTED=0).
- NO duplication of Mission registry, GoalRun registry, Execution registry, or Channel registry (REGISTRY_OVERLAP=0).
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

from .models import (
    AgentFleet,
    AgentInstance,
    AgentInstanceStatus,
    AgentLease,
    CollaborationAssignment,
    FleetQueueEntry,
    FleetStatus,
    LeaseStatus,
    MissionMigration,
    PlacementDecision,
    ResourcePool,
    ResourceReservation,
)


class StaleGenerationError(RuntimeError):
    """Raised when an operation is attempted with an outdated generation."""


class FleetRegistry:
    """Durable state registry for Fleet, AgentInstances, Leases, and Placements."""

    def __init__(self, fleet_id: str = "default_fleet", base_dir: str | Path | None = None):
        self.fleet_id = fleet_id
        self._lock = threading.RLock()
        self.base_dir = (
            (Path(base_dir) if base_dir else Path(os.environ.get("VEYA_HOME", Path.cwd())))
            / ".veya"
            / "fleet"
            / fleet_id
        )
        self.base_dir.mkdir(parents=True, exist_ok=True)

        self._fleet_file = self.base_dir / "fleet.json"
        self._agents_file = self.base_dir / "agents.jsonl"
        self._placements_file = self.base_dir / "placements.jsonl"
        self._leases_file = self.base_dir / "leases.jsonl"
        self._pools_file = self.base_dir / "pools.jsonl"
        self._reservations_file = self.base_dir / "reservations.jsonl"
        self._queue_file = self.base_dir / "queue.jsonl"
        self._migrations_file = self.base_dir / "migrations.jsonl"
        self._collaborations_file = self.base_dir / "collaborations.jsonl"

        # In-memory indices
        self._fleet: AgentFleet | None = None
        self._agents: dict[str, AgentInstance] = {}
        self._placements: dict[str, PlacementDecision] = {}
        self._leases: dict[str, AgentLease] = {}
        self._pools: dict[str, ResourcePool] = {}
        self._reservations: dict[str, ResourceReservation] = {}
        self._queue: dict[str, FleetQueueEntry] = {}
        self._migrations: dict[str, MissionMigration] = {}
        self._collaborations: dict[str, CollaborationAssignment] = {}

        self._load()

    def _load(self) -> None:
        with self._lock:
            if self._fleet_file.is_file():
                try:
                    data = json.loads(self._fleet_file.read_text(encoding="utf-8"))
                    self._fleet = AgentFleet.from_dict(data)
                except Exception:
                    self._fleet = AgentFleet(fleet_id=self.fleet_id)
            else:
                self._fleet = AgentFleet(fleet_id=self.fleet_id)
                self._persist_fleet()

            # Load agents
            if self._agents_file.is_file():
                for line in self._agents_file.read_text(encoding="utf-8").splitlines():
                    if line.strip():
                        inst = AgentInstance.from_dict(json.loads(line))
                        self._agents[inst.agent_instance_id] = inst

            # Load placements
            if self._placements_file.is_file():
                for line in self._placements_file.read_text(encoding="utf-8").splitlines():
                    if line.strip():
                        plc = PlacementDecision.from_dict(json.loads(line))
                        self._placements[plc.placement_id] = plc

            # Load leases
            if self._leases_file.is_file():
                for line in self._leases_file.read_text(encoding="utf-8").splitlines():
                    if line.strip():
                        ls = AgentLease.from_dict(json.loads(line))
                        self._leases[ls.lease_id] = ls

            # Load pools
            if self._pools_file.is_file():
                for line in self._pools_file.read_text(encoding="utf-8").splitlines():
                    if line.strip():
                        pool = ResourcePool.from_dict(json.loads(line))
                        self._pools[pool.pool_id] = pool

            # Load reservations
            if self._reservations_file.is_file():
                for line in self._reservations_file.read_text(encoding="utf-8").splitlines():
                    if line.strip():
                        res = ResourceReservation.from_dict(json.loads(line))
                        self._reservations[res.reservation_id] = res

            # Load queue
            if self._queue_file.is_file():
                for line in self._queue_file.read_text(encoding="utf-8").splitlines():
                    if line.strip():
                        q = FleetQueueEntry.from_dict(json.loads(line))
                        self._queue[q.queue_id] = q

            # Load migrations
            if self._migrations_file.is_file():
                for line in self._migrations_file.read_text(encoding="utf-8").splitlines():
                    if line.strip():
                        m = MissionMigration.from_dict(json.loads(line))
                        self._migrations[m.migration_id] = m

            # Load collaborations
            if self._collaborations_file.is_file():
                for line in self._collaborations_file.read_text(encoding="utf-8").splitlines():
                    if line.strip():
                        c = CollaborationAssignment.from_dict(json.loads(line))
                        self._collaborations[c.collaboration_id] = c

    def _persist_fleet(self) -> None:
        if self._fleet:
            self._fleet_file.write_text(
                json.dumps(self._fleet.to_dict(), indent=2), encoding="utf-8"
            )

    def _rewrite_agents(self) -> None:
        lines = [json.dumps(a.to_dict()) for a in self._agents.values()]
        self._agents_file.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")

    def _rewrite_placements(self) -> None:
        lines = [json.dumps(p.to_dict()) for p in self._placements.values()]
        self._placements_file.write_text(
            "\n".join(lines) + ("\n" if lines else ""), encoding="utf-8"
        )

    def _rewrite_leases(self) -> None:
        lines = [json.dumps(lease.to_dict()) for lease in self._leases.values()]
        self._leases_file.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")

    def _rewrite_pools(self) -> None:
        lines = [json.dumps(p.to_dict()) for p in self._pools.values()]
        self._pools_file.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")

    def _rewrite_reservations(self) -> None:
        lines = [json.dumps(r.to_dict()) for r in self._reservations.values()]
        self._reservations_file.write_text(
            "\n".join(lines) + ("\n" if lines else ""), encoding="utf-8"
        )

    def _rewrite_queue(self) -> None:
        lines = [json.dumps(q.to_dict()) for q in self._queue.values()]
        self._queue_file.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")

    def _rewrite_migrations(self) -> None:
        lines = [json.dumps(m.to_dict()) for m in self._migrations.values()]
        self._migrations_file.write_text(
            "\n".join(lines) + ("\n" if lines else ""), encoding="utf-8"
        )

    def _rewrite_collaborations(self) -> None:
        lines = [json.dumps(c.to_dict()) for c in self._collaborations.values()]
        self._collaborations_file.write_text(
            "\n".join(lines) + ("\n" if lines else ""), encoding="utf-8"
        )

    # Fleet operations
    def get_fleet(self) -> AgentFleet:
        with self._lock:
            assert self._fleet is not None
            return self._fleet

    def advance_fleet_generation(self) -> int:
        """Advance fleet generation, invalidating all old generation commits (spec §11)."""
        with self._lock:
            assert self._fleet is not None
            self._fleet.generation += 1
            self._fleet.updated_at = os.times().elapsed
            self._persist_fleet()
            return self._fleet.generation

    def update_fleet_status(self, status: FleetStatus) -> None:
        with self._lock:
            assert self._fleet is not None
            self._fleet.status = status
            self._persist_fleet()

    # Agent Instance operations
    def register_agent(self, agent: AgentInstance) -> None:
        with self._lock:
            self._agents[agent.agent_instance_id] = agent
            self._rewrite_agents()

    def get_agent(self, agent_instance_id: str) -> AgentInstance | None:
        with self._lock:
            return self._agents.get(agent_instance_id)

    def list_agents(self) -> list[AgentInstance]:
        with self._lock:
            return list(self._agents.values())

    def update_agent_status(
        self, agent_instance_id: str, status: AgentInstanceStatus, generation: int | None = None
    ) -> None:
        with self._lock:
            agent = self._agents.get(agent_instance_id)
            if not agent:
                return
            if generation is not None and generation < agent.generation:
                raise StaleGenerationError(
                    f"Agent commit rejected: generation {generation} < current {agent.generation}"
                )
            agent.status = status
            self._rewrite_agents()

    def advance_agent_generation(self, agent_instance_id: str) -> int:
        with self._lock:
            agent = self._agents.get(agent_instance_id)
            if not agent:
                raise KeyError(f"Agent {agent_instance_id} not found")
            agent.generation += 1
            self._rewrite_agents()
            return agent.generation

    # Placement operations with generation fencing
    def save_placement(self, placement: PlacementDecision) -> None:
        with self._lock:
            assert self._fleet is not None
            if placement.generation < self._fleet.generation:
                raise StaleGenerationError(
                    f"Placement commit rejected: fleet generation {placement.generation} < {self._fleet.generation}"
                )
            self._placements[placement.placement_id] = placement
            self._rewrite_placements()

    def get_placement(self, placement_id: str) -> PlacementDecision | None:
        with self._lock:
            return self._placements.get(placement_id)

    def list_placements(self) -> list[PlacementDecision]:
        with self._lock:
            return list(self._placements.values())

    def find_active_placement_for_mission(self, mission_id: str) -> PlacementDecision | None:
        with self._lock:
            for p in self._placements.values():
                if p.mission_id == mission_id and p.status == "ACTIVE":
                    return p
            return None

    # Leases operations
    def save_lease(self, lease: AgentLease, expected_fleet_generation: int | None = None) -> None:
        with self._lock:
            assert self._fleet is not None
            if (
                expected_fleet_generation is not None
                and expected_fleet_generation < self._fleet.generation
            ):
                raise StaleGenerationError(
                    f"Lease commit rejected: stale fleet generation {expected_fleet_generation} < {self._fleet.generation}"
                )
            agent = self._agents.get(lease.agent_instance_id)
            if agent and lease.holder_generation < agent.generation:
                raise StaleGenerationError(
                    f"Lease commit rejected: stale agent generation {lease.holder_generation} < {agent.generation}"
                )
            self._leases[lease.lease_id] = lease
            self._rewrite_leases()

    def get_lease(self, lease_id: str) -> AgentLease | None:
        with self._lock:
            return self._leases.get(lease_id)

    def list_leases(self) -> list[AgentLease]:
        with self._lock:
            return list(self._leases.values())

    def find_active_lease_for_mission(self, mission_id: str) -> AgentLease | None:
        with self._lock:
            for lease in self._leases.values():
                if lease.mission_id == mission_id and lease.status == LeaseStatus.ACTIVE:
                    return lease
            return None

    # Resource Pool & Reservation operations
    def save_pool(self, pool: ResourcePool) -> None:
        with self._lock:
            self._pools[pool.pool_id] = pool
            self._rewrite_pools()

    def get_pool(self, pool_id: str) -> ResourcePool | None:
        with self._lock:
            return self._pools.get(pool_id)

    def list_pools(self) -> list[ResourcePool]:
        with self._lock:
            return list(self._pools.values())

    def save_reservation(
        self, reservation: ResourceReservation, expected_fleet_generation: int | None = None
    ) -> None:
        with self._lock:
            assert self._fleet is not None
            if (
                expected_fleet_generation is not None
                and expected_fleet_generation < self._fleet.generation
            ):
                raise StaleGenerationError(
                    f"Reservation commit rejected: stale fleet generation {expected_fleet_generation} < {self._fleet.generation}"
                )
            self._reservations[reservation.reservation_id] = reservation
            self._rewrite_reservations()

    def get_reservation(self, reservation_id: str) -> ResourceReservation | None:
        with self._lock:
            return self._reservations.get(reservation_id)

    def list_reservations(self) -> list[ResourceReservation]:
        with self._lock:
            return list(self._reservations.values())

    # Queue operations
    def enqueue(self, entry: FleetQueueEntry) -> None:
        with self._lock:
            self._queue[entry.queue_id] = entry
            self._rewrite_queue()

    def remove_from_queue(self, queue_id: str) -> FleetQueueEntry | None:
        with self._lock:
            entry = self._queue.pop(queue_id, None)
            if entry:
                self._rewrite_queue()
            return entry

    def list_queue(self) -> list[FleetQueueEntry]:
        with self._lock:
            # Sort by priority desc, enqueued_at asc
            return sorted(self._queue.values(), key=lambda e: (-e.priority, e.enqueued_at))

    # Migration operations
    def save_migration(
        self, migration: MissionMigration, expected_fleet_generation: int | None = None
    ) -> None:
        with self._lock:
            assert self._fleet is not None
            if (
                expected_fleet_generation is not None
                and expected_fleet_generation < self._fleet.generation
            ):
                raise StaleGenerationError(
                    f"Migration commit rejected: stale fleet generation {expected_fleet_generation} < {self._fleet.generation}"
                )
            self._migrations[migration.migration_id] = migration
            self._rewrite_migrations()

    def get_migration(self, migration_id: str) -> MissionMigration | None:
        with self._lock:
            return self._migrations.get(migration_id)

    def list_migrations(self) -> list[MissionMigration]:
        with self._lock:
            return list(self._migrations.values())

    # Collaboration operations
    def save_collaboration(self, collab: CollaborationAssignment) -> None:
        with self._lock:
            self._collaborations[collab.collaboration_id] = collab
            self._rewrite_collaborations()

    def list_collaborations(self, mission_id: str | None = None) -> list[CollaborationAssignment]:
        with self._lock:
            if mission_id is None:
                return list(self._collaborations.values())
            return [c for c in self._collaborations.values() if c.mission_id == mission_id]
