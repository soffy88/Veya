"""Fleet Lifecycle, Health Monitoring, and Drain (spec §22, §23, §28, §29, §59).

Invariants:
- Agent heartbeats verify runtime health without mutating semantic mission state.
- Unhealthy agents transition to UNAVAILABLE, expiring orphaned leases.
- Draining stops new admissions/placements and safely concludes running work.
- Fleet drain stops admissions and quiesces all agents to STOPPED.
"""

from __future__ import annotations

import threading
import time

from .leases import LeaseManager
from .models import AgentInstance, AgentInstanceStatus, FleetStatus, LeaseStatus
from .registry import FleetRegistry


class FleetLifecycleManager:
    """Oversees agent heartbeats, health probes, failure detection, and graceful drain."""

    def __init__(
        self,
        registry: FleetRegistry,
        lease_manager: LeaseManager,
        heartbeat_timeout_s: float = 30.0,
    ):
        self.registry = registry
        self.lease_manager = lease_manager
        self.heartbeat_timeout_s = heartbeat_timeout_s
        self._lock = threading.RLock()

    def record_heartbeat(
        self, agent_instance_id: str, generation: int | None = None
    ) -> AgentInstance:
        """Process heartbeat from an agent instance."""
        with self._lock:
            agent = self.registry.get_agent(agent_instance_id)
            if not agent:
                raise KeyError(f"Agent '{agent_instance_id}' not found")

            # Check generation fencing
            if generation is not None and generation < agent.generation:
                return agent

            agent.last_heartbeat_at = time.time()
            if agent.status == AgentInstanceStatus.UNAVAILABLE:
                # Agent recovered
                agent.status = AgentInstanceStatus.READY
            self.registry.register_agent(agent)
            return agent

    def detect_failures(self) -> list[str]:
        """Detect heartbeat timeouts and mark dead agents as UNAVAILABLE (spec §23)."""
        with self._lock:
            now = time.time()
            failed_agents: list[str] = []
            for agent in self.registry.list_agents():
                if (
                    agent.status in (AgentInstanceStatus.READY, AgentInstanceStatus.BUSY)
                    and now - agent.last_heartbeat_at > self.heartbeat_timeout_s
                ):
                    agent.status = AgentInstanceStatus.UNAVAILABLE
                    self.registry.register_agent(agent)
                    failed_agents.append(agent.agent_instance_id)

                    # Expire orphaned leases held by dead agent
                    for lease in self.registry.list_leases():
                        if (
                            lease.agent_instance_id == agent.agent_instance_id
                            and lease.status == LeaseStatus.ACTIVE
                        ):
                            lease.status = LeaseStatus.EXPIRED
                            self.registry.save_lease(lease)

            return failed_agents

    def drain_agent(self, agent_instance_id: str) -> None:
        """Mark an agent instance for graceful drain (spec §28)."""
        with self._lock:
            agent = self.registry.get_agent(agent_instance_id)
            if not agent:
                raise KeyError(f"Agent '{agent_instance_id}' not found")

            agent.status = AgentInstanceStatus.DRAINING
            self.registry.register_agent(agent)
            self._check_agent_drain_completion(agent_instance_id)

    def _check_agent_drain_completion(self, agent_instance_id: str) -> bool:
        """If agent has no active leases, mark it STOPPED."""
        agent = self.registry.get_agent(agent_instance_id)
        if not agent or agent.status != AgentInstanceStatus.DRAINING:
            return False

        active_leases = [
            lease
            for lease in self.registry.list_leases()
            if lease.agent_instance_id == agent_instance_id and lease.status == LeaseStatus.ACTIVE
        ]
        if not active_leases:
            agent.status = AgentInstanceStatus.STOPPED
            self.registry.register_agent(agent)
            return True
        return False

    def drain_fleet(self) -> None:
        """Initiate graceful drain of the entire fleet (spec §29)."""
        with self._lock:
            self.registry.update_fleet_status(FleetStatus.DRAINING)
            for agent in self.registry.list_agents():
                if agent.status not in (
                    AgentInstanceStatus.STOPPED,
                    AgentInstanceStatus.UNAVAILABLE,
                ):
                    self.drain_agent(agent.agent_instance_id)

    def sweep_draining_agents(self) -> int:
        """Check all draining agents and stop those with zero active leases."""
        with self._lock:
            stopped_count = 0
            for agent in self.registry.list_agents():
                if (
                    agent.status == AgentInstanceStatus.DRAINING
                    and self._check_agent_drain_completion(agent.agent_instance_id)
                ):
                    stopped_count += 1
            return stopped_count
