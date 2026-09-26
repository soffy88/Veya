"""Fleet Scheduler with Capacity, Capability, and Fairness (spec §7, §8, §17-19, §41-43, §48-49, §57).

Invariants:
- Single active placement per (mission, generation) (DUPLICATE_PLACEMENT=0).
- Candidate filtering ensures capability validity (CAPABILITY_INVALID_PLACEMENT=0).
- Aging-based fairness prevents starvation (STARVATION=0).
- Circuit breaker avoids placement thrash on failing targets.
- Scheduler only makes placement decisions, never executes semantic work.
"""

from __future__ import annotations

import time
import uuid

from .admission import FleetAdmissionController
from .models import (
    AdmissionOutcome,
    AgentInstance,
    AgentInstanceStatus,
    FleetQueueEntry,
    PlacementDecision,
    PlacementRequest,
    SchedulingPolicy,
)
from .registry import FleetRegistry


class FleetScheduler:
    """Schedules missions to capable and available agent instances."""

    def __init__(
        self,
        registry: FleetRegistry,
        admission_controller: FleetAdmissionController | None = None,
        policy: SchedulingPolicy = SchedulingPolicy.CAPABILITY_AWARE,
        aging_interval_s: float = 30.0,
        circuit_cooldown_s: float = 60.0,
    ):
        self.registry = registry
        self.admission = admission_controller or FleetAdmissionController(registry)
        self.policy = policy
        self.aging_interval_s = aging_interval_s
        self.circuit_cooldown_s = circuit_cooldown_s

        # Target circuit breaker memory (spec §48, §49): target_id -> cooldown_until
        self._circuit_memory: dict[str, float] = {}

    def record_target_failure(self, agent_instance_id: str) -> None:
        """Record an infrastructure placement failure for circuit breaking."""
        self._circuit_memory[agent_instance_id] = time.time() + self.circuit_cooldown_s

    def is_target_available(self, agent_instance_id: str) -> bool:
        cooldown = self._circuit_memory.get(agent_instance_id, 0.0)
        return time.time() >= cooldown

    def calculate_effective_priority(
        self, entry: FleetQueueEntry, now: float | None = None
    ) -> float:
        """Calculate effective priority with anti-starvation aging (spec §19)."""
        current_time = now or time.time()
        age = max(0.0, current_time - entry.enqueued_at)
        aging_boost = (age / self.aging_interval_s) * 5.0
        return float(entry.priority) + aging_boost

    def enqueue(self, request: PlacementRequest, defer_reason: str = "") -> FleetQueueEntry:
        fleet = self.registry.get_fleet()
        entry = FleetQueueEntry(
            queue_id=f"q_{uuid.uuid4().hex[:12]}",
            mission_id=request.mission_id,
            principal_id=request.principal_id,
            requirements=request.to_dict(),
            priority=request.priority,
            enqueued_at=time.time(),
            defer_reason=defer_reason,
            attempt=0,
            generation=fleet.generation,
        )
        self.registry.enqueue(entry)
        return entry

    def find_candidates(self, request: PlacementRequest) -> list[AgentInstance]:
        """Find and filter candidate agents matching capability and availability."""
        all_agents = self.registry.list_agents()
        candidates: list[AgentInstance] = []

        for agent in all_agents:
            # 1. Status check: must be READY or BUSY
            if agent.status not in (AgentInstanceStatus.READY, AgentInstanceStatus.BUSY):
                continue

            # 2. Circuit breaker check
            if not self.is_target_available(agent.agent_instance_id):
                continue

            # 3. Capability check: candidate must have ALL required capabilities (CAPABILITY_INVALID_PLACEMENT=0)
            if not all(cap in agent.capability_profile for cap in request.required_capabilities):
                continue

            # 4. Preferred runtime check if specified
            if request.preferred_runtime and agent.runtime_id != request.preferred_runtime:
                continue

            candidates.append(agent)

        return candidates

    def score_candidate(self, agent: AgentInstance, request: PlacementRequest) -> float:
        """Score candidate for least-loaded and locality-aware policies."""
        score = 0.0

        # Load factor: count active placements assigned to this agent
        active_on_agent = 0
        for p in self.registry.list_placements():
            if p.agent_instance_id == agent.agent_instance_id and p.status == "ACTIVE":
                active_on_agent += 1

        # Lower load gets higher score
        score -= active_on_agent * 10.0

        # Locality factor: workspace scope or locality match
        ws_req = request.workspace_requirements
        ws_path = ws_req.get("workspace_path") or ws_req.get("workspace_id")
        if ws_path and ws_path in agent.workspace_scope:
            score += 25.0

        if request.locality and (
            request.locality == agent.placement or request.locality == agent.host_id
        ):
            score += 15.0

        return score

    def schedule_mission(
        self, request: PlacementRequest, auto_enqueue_on_defer: bool = True
    ) -> tuple[PlacementDecision | None, str]:
        """Schedule a mission to an agent instance."""
        fleet = self.registry.get_fleet()

        # 1. Idempotency check: if active placement exists for this mission, return it (DUPLICATE_PLACEMENT=0)
        existing = self.registry.find_active_placement_for_mission(request.mission_id)
        if existing is not None:
            return existing, "IDEMPOTENT_EXISTING_PLACEMENT"

        # 2. Admission control
        decision = self.admission.evaluate(request)
        if decision.outcome == AdmissionOutcome.REJECT:
            return None, f"REJECTED: {decision.reason}"

        if decision.outcome == AdmissionOutcome.DEFER:
            if auto_enqueue_on_defer:
                self.enqueue(request, defer_reason=decision.defer_reason)
            return None, f"DEFERRED: {decision.defer_reason}: {decision.reason}"

        # 3. Candidate filtering
        candidates = self.find_candidates(request)
        if not candidates:
            if auto_enqueue_on_defer:
                self.enqueue(request, defer_reason="NO_CANDIDATE_MATCH")
            return None, "DEFERRED: No available candidate matching requirements"

        # 4. Rank candidates by policy
        ranked = sorted(candidates, key=lambda a: self.score_candidate(a, request), reverse=True)
        chosen_agent = ranked[0]

        # 5. Commit placement decision with fleet generation
        plc_id = f"plc_{uuid.uuid4().hex[:12]}"
        placement = PlacementDecision(
            placement_id=plc_id,
            mission_id=request.mission_id,
            agent_instance_id=chosen_agent.agent_instance_id,
            runtime_id=chosen_agent.runtime_id,
            reason=f"Scheduled by {self.policy.value} to {chosen_agent.agent_instance_id}",
            manifest_snapshot=request.to_dict(),
            resource_reservation_id="",
            created_at=time.time(),
            generation=fleet.generation,
            status="ACTIVE",
        )

        self.registry.save_placement(placement)
        return placement, "PLACED"

    def schedule_next_queued(self) -> tuple[PlacementDecision | None, FleetQueueEntry | None]:
        """Attempt to schedule the next highest priority queued mission (spec §19, §41)."""
        queue = self.registry.list_queue()
        if not queue:
            return None, None

        now = time.time()
        # Sort queue by effective priority descending
        ranked_queue = sorted(
            queue, key=lambda e: self.calculate_effective_priority(e, now), reverse=True
        )

        for entry in ranked_queue:
            req = PlacementRequest.from_dict(entry.requirements)
            decision, status = self.schedule_mission(req, auto_enqueue_on_defer=False)
            if decision is not None:
                # Successfully placed, remove from queue
                self.registry.remove_from_queue(entry.queue_id)
                return decision, entry
            else:
                # Still deferred, update attempt count
                entry.attempt += 1
                entry.defer_reason = status
                self.registry.enqueue(entry)

        return None, None
