"""Fleet Agent Leases and Generation Fencing (spec §10, §11, §59).

Invariants:
- ONE_ACTIVE_AGENT_LEASE_PER_MISSION=1: Only one active lease per mission unless parallel mode enabled.
- Generation fencing strictly enforced against stale generations (STALE_AGENT_COMMIT_ACCEPTED=0, STALE_FLEET_COMMIT_ACCEPTED=0).
- Leases are durable and track heartbeats with automatic expiration.
"""

from __future__ import annotations

import threading
import time
import uuid

from .models import AgentInstanceStatus, AgentLease, LeaseStatus
from .registry import FleetRegistry, StaleGenerationError


class LeaseConflictError(RuntimeError):
    """Raised when acquiring a lease violates exclusivity or status rules."""


class LeaseManager:
    """Manages acquisition, heartbeating, expiration, and release of agent leases."""

    def __init__(self, registry: FleetRegistry, default_lease_ttl_s: float = 60.0):
        self.registry = registry
        self.default_lease_ttl_s = default_lease_ttl_s
        self._lock = threading.RLock()

    def acquire_lease(
        self,
        agent_instance_id: str,
        mission_id: str,
        allow_parallel: bool = False,
        ttl_s: float | None = None,
    ) -> AgentLease:
        """Acquire a durable lease binding an agent instance to a mission."""
        with self._lock:
            # 1. Enforce ONE_ACTIVE_AGENT_LEASE_PER_MISSION=1
            existing = self.registry.find_active_lease_for_mission(mission_id)
            if existing is not None and not allow_parallel:
                raise LeaseConflictError(
                    f"Mission '{mission_id}' already has active lease '{existing.lease_id}' on agent '{existing.agent_instance_id}'"
                )

            # 2. Check agent status and generation
            agent = self.registry.get_agent(agent_instance_id)
            if not agent:
                raise KeyError(f"Agent '{agent_instance_id}' not found")
            if agent.status not in (AgentInstanceStatus.READY, AgentInstanceStatus.BUSY):
                raise LeaseConflictError(
                    f"Agent '{agent_instance_id}' cannot acquire lease in status '{agent.status.value}'"
                )

            fleet = self.registry.get_fleet()
            lease_ttl = ttl_s or self.default_lease_ttl_s
            now = time.time()
            lease_id = f"lease_{uuid.uuid4().hex[:12]}"

            lease = AgentLease(
                lease_id=lease_id,
                agent_instance_id=agent_instance_id,
                mission_id=mission_id,
                holder_generation=agent.generation,
                acquired_at=now,
                heartbeat_at=now,
                expires_at=now + lease_ttl,
                status=LeaseStatus.ACTIVE,
            )

            # Save lease with generation fencing
            self.registry.save_lease(lease, expected_fleet_generation=fleet.generation)

            # Mark agent as busy if it was ready
            if agent.status == AgentInstanceStatus.READY:
                self.registry.update_agent_status(agent_instance_id, AgentInstanceStatus.BUSY)

            return lease

    def heartbeat_lease(
        self, lease_id: str, expected_agent_generation: int | None = None
    ) -> AgentLease:
        """Renew lease heartbeat and extend expiration."""
        with self._lock:
            lease = self.registry.get_lease(lease_id)
            if not lease:
                raise KeyError(f"Lease '{lease_id}' not found")
            if lease.status != LeaseStatus.ACTIVE:
                raise LeaseConflictError(
                    f"Cannot heartbeat inactive lease in status '{lease.status.value}'"
                )

            agent = self.registry.get_agent(lease.agent_instance_id)
            if agent:
                if (
                    expected_agent_generation is not None
                    and expected_agent_generation < agent.generation
                ):
                    raise StaleGenerationError(
                        f"Heartbeat rejected: agent generation {expected_agent_generation} < {agent.generation}"
                    )
                if lease.holder_generation < agent.generation:
                    raise StaleGenerationError(
                        f"Heartbeat rejected: lease generation {lease.holder_generation} < {agent.generation}"
                    )

            now = time.time()
            lease.heartbeat_at = now
            lease.expires_at = now + self.default_lease_ttl_s
            self.registry.save_lease(lease)
            return lease

    def release_lease(self, lease_id: str) -> None:
        """Release an active lease."""
        with self._lock:
            lease = self.registry.get_lease(lease_id)
            if not lease or lease.status in (LeaseStatus.RELEASED, LeaseStatus.EXPIRED):
                return
            lease.status = LeaseStatus.RELEASED
            self.registry.save_lease(lease)

            # Check if agent has any remaining active leases
            active_on_agent = [
                als
                for als in self.registry.list_leases()
                if als.agent_instance_id == lease.agent_instance_id
                and als.status == LeaseStatus.ACTIVE
            ]
            if not active_on_agent:
                agent = self.registry.get_agent(lease.agent_instance_id)
                if agent and agent.status == AgentInstanceStatus.BUSY:
                    self.registry.update_agent_status(
                        lease.agent_instance_id, AgentInstanceStatus.READY
                    )

    def sweep_expired_leases(self) -> int:
        """Expire all leases whose ttl has passed (spec §10, LEASE_LEAK=0)."""
        with self._lock:
            now = time.time()
            expired_count = 0
            for lease in self.registry.list_leases():
                if lease.status == LeaseStatus.ACTIVE and now > lease.expires_at:
                    lease.status = LeaseStatus.EXPIRED
                    self.registry.save_lease(lease)
                    expired_count += 1
            return expired_count
