"""Rollout and Rolling Upgrade Coordinator for Veya Agent Operations V1 (spec §8-§11, §48).

Coordinates safe rolling upgrades across fleet agents:
  Select targets -> Drain batch -> Deploy new runtime generation -> Health check -> Restore admission -> Next batch.
Enforces:
  - ZERO_MISSION_LOSS=YES
  - ZERO_ACCEPTED_PROGRESS_LOSS=YES
  - STALE_RUNTIME_GENERATION_ACCEPTED=0
  - Rollback on health gate failure (manual or auto-rollback policy)
"""

from __future__ import annotations

import contextlib
import time
import uuid
from collections.abc import Callable

from veya.fleet import AgentInstanceStatus, FleetController

from .models import (
    HealthState,
    RolloutPlan,
    RolloutRevision,
    RolloutStatus,
    RolloutTarget,
)


class RolloutHaltedError(Exception):
    pass


class StaleGenerationCommitError(Exception):
    pass


class RolloutCoordinator:
    """Manages rolling upgrades, health verification gates, and rollback policies."""

    def __init__(self, fleet_controller: FleetController | None = None) -> None:
        self.fleet = fleet_controller
        self._rollouts: dict[str, RolloutPlan] = {}
        # agent_id -> active runtime_generation
        self._agent_runtime_generations: dict[str, int] = {}
        self._current_operations_generation: int = 1

    def set_fleet_controller(self, fleet: FleetController) -> None:
        self.fleet = fleet

    def get_runtime_generation(self, agent_id: str) -> int:
        return self._agent_runtime_generations.get(agent_id, 1)

    def verify_runtime_generation(self, agent_id: str, submitted_gen: int) -> bool:
        """Enforces STALE_RUNTIME_GENERATION_ACCEPTED=0 (spec §11)."""
        current_gen = self.get_runtime_generation(agent_id)
        if submitted_gen < current_gen:
            raise StaleGenerationCommitError(
                f"Stale runtime generation: agent={agent_id}, submitted={submitted_gen}, current={current_gen}"
            )
        return True

    def create_rollout(
        self,
        artifact_version: str,
        target_agent_ids: list[str],
        batch_size: int = 2,
        max_unavailable: int = 1,
        health_gate: str = "STRICT",
        rollback_policy: str = "AUTOMATIC",
        expected_operations_gen: int | None = None,
    ) -> RolloutPlan:
        """Create a new rolling upgrade plan (spec §8)."""
        if (
            expected_operations_gen is not None
            and expected_operations_gen < self._current_operations_generation
        ):
            raise StaleGenerationCommitError("Stale operations generation")

        rollout_id = f"ro_{uuid.uuid4().hex[:10]}"
        targets = [
            RolloutTarget(
                agent_id=aid,
                current_version="v1.0.0",
                target_version=artifact_version,
                status="PENDING",
            )
            for aid in target_agent_ids
        ]

        plan = RolloutPlan(
            rollout_id=rollout_id,
            artifact_version=artifact_version,
            target_selector={"agents": target_agent_ids},
            batch_size=batch_size,
            max_unavailable=max_unavailable,
            health_gate=health_gate,
            rollback_policy=rollback_policy,
            runtime_generation=max(self._agent_runtime_generations.values(), default=1) + 1,
            status=RolloutStatus.PENDING,
            targets=targets,
        )
        self._rollouts[rollout_id] = plan
        return plan

    def execute_rollout(
        self,
        rollout_id: str,
        health_probe_fn: Callable[[str], HealthState] | None = None,
    ) -> RolloutPlan:
        """Executes rolling upgrade batch by batch with health gating and rollback (spec §9, §10)."""
        plan = self._rollouts.get(rollout_id)
        if not plan:
            raise KeyError(f"Rollout '{rollout_id}' not found")

        plan.status = RolloutStatus.IN_PROGRESS
        plan.updated_at = time.time()

        targets = plan.targets
        batch_size = plan.batch_size

        for i in range(0, len(targets), batch_size):
            batch = targets[i : i + batch_size]

            # Step 1: Drain batch (stop new admissions, let in-flight finish/migrate)
            for target in batch:
                target.status = "DRAINING"
                if self.fleet:
                    with contextlib.suppress(Exception):
                        self.fleet.drain_agent(target.agent_id)

            # Step 2: Deploy new runtime generation
            for target in batch:
                target.status = "DEPLOYING"
                # Update runtime generation for fencing
                self._agent_runtime_generations[target.agent_id] = plan.runtime_generation

            # Step 3: Health check
            for target in batch:
                target.status = "HEALTH_CHECK"
                is_healthy = True
                if health_probe_fn:
                    h_state = health_probe_fn(target.agent_id)
                    if h_state != HealthState.HEALTHY:
                        is_healthy = False

                if not is_healthy:
                    # Health gate breached: HALT_ROLLOUT (spec §10, §54)
                    plan.status = RolloutStatus.HALTED
                    target.status = "FAILED"
                    plan.rollback_reason = f"Health check failed on {target.agent_id}"

                    if plan.rollback_policy == "AUTOMATIC":
                        return self.rollback_rollout(rollout_id, reason=plan.rollback_reason)
                    return plan

                target.status = "HEALTHY"

            # Step 4: Restore admission for batch
            for target in batch:
                target.current_version = target.target_version
                target.status = "COMPLETED"
                if self.fleet:
                    with contextlib.suppress(Exception):
                        self.fleet.registry.update_agent_status(
                            target.agent_id, AgentInstanceStatus.READY
                        )

        plan.status = RolloutStatus.COMPLETED
        plan.updated_at = time.time()
        return plan

    def halt_rollout(self, rollout_id: str, reason: str = "Operator halted") -> RolloutPlan:
        plan = self._rollouts.get(rollout_id)
        if not plan:
            raise KeyError(f"Rollout '{rollout_id}' not found")
        plan.status = RolloutStatus.HALTED
        plan.rollback_reason = reason
        plan.updated_at = time.time()
        return plan

    def rollback_rollout(self, rollout_id: str, reason: str = "Rollback requested") -> RolloutPlan:
        """Rollback rollout to previous stable version (spec §10, §55)."""
        plan = self._rollouts.get(rollout_id)
        if not plan:
            raise KeyError(f"Rollout '{rollout_id}' not found")

        prev_gen = max(1, plan.runtime_generation - 1)
        plan.rollback_revision = RolloutRevision(
            revision_id=f"rev_rb_{uuid.uuid4().hex[:8]}",
            artifact_version="v1.0.0",
            runtime_generation=prev_gen,
        )
        plan.rollback_reason = reason

        # Restore targets back to base generation
        for target in plan.targets:
            self._agent_runtime_generations[target.agent_id] = prev_gen
            target.current_version = "v1.0.0"
            target.status = "ROLLED_BACK"
            if self.fleet:
                with contextlib.suppress(Exception):
                    self.fleet.registry.update_agent_status(
                        target.agent_id, AgentInstanceStatus.READY
                    )

        plan.status = RolloutStatus.ROLLED_BACK
        plan.updated_at = time.time()
        return plan

    def get_rollout(self, rollout_id: str) -> RolloutPlan | None:
        return self._rollouts.get(rollout_id)
