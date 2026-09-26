"""Health projection and Service Level Objective (SLO) evaluation engine for Operations V1 (spec §12-§15).

Projects operational health states:
  HEALTHY, DEGRADED, UNHEALTHY, UNKNOWN
Evaluates SLOs across rolling/fixed windows:
  Outputs SLI_VALUE, SLO_TARGET, ERROR_BUDGET_REMAINING, BREACH.
Maintains strict authority boundaries:
  Health & SLO do NOT alter Mission outcomes or GoalRun semantics.
"""

from __future__ import annotations

import time

from .models import (
    HealthPolicy,
    HealthState,
    SLODefinition,
    SLOEvaluation,
)


class HealthEvaluator:
    """Evaluates agent, fleet, and provider operational health based on telemetry signals."""

    def __init__(self, default_policy: HealthPolicy | None = None) -> None:
        self.default_policy = default_policy or HealthPolicy(policy_id="default_health")

    def evaluate_agent_health(
        self,
        last_heartbeat_s_ago: float,
        error_rate: float,
        latency_s: float,
        lease_failures: int = 0,
        execution_failures: int = 0,
        policy: HealthPolicy | None = None,
    ) -> tuple[HealthState, str]:
        p = policy or self.default_policy

        if last_heartbeat_s_ago > p.heartbeat_timeout_s * 2:
            return HealthState.UNHEALTHY, f"Heartbeat lost ({last_heartbeat_s_ago:.1f}s ago)"
        if last_heartbeat_s_ago > p.heartbeat_timeout_s:
            return HealthState.DEGRADED, f"Heartbeat delayed ({last_heartbeat_s_ago:.1f}s ago)"

        if lease_failures >= p.lease_failure_threshold:
            return HealthState.UNHEALTHY, f"Excessive lease failures ({lease_failures})"
        if execution_failures >= p.execution_failure_threshold:
            return HealthState.UNHEALTHY, f"Excessive execution failures ({execution_failures})"

        if error_rate > p.error_rate_threshold * 2:
            return HealthState.UNHEALTHY, f"High error rate ({error_rate:.1%})"
        if error_rate > p.error_rate_threshold:
            return HealthState.DEGRADED, f"Elevated error rate ({error_rate:.1%})"

        if latency_s > p.latency_threshold_s * 2:
            return HealthState.DEGRADED, f"High latency ({latency_s:.2f}s)"

        return HealthState.HEALTHY, "All operational telemetry within thresholds"

    def evaluate_fleet_health(
        self,
        agent_health_states: list[HealthState],
        queue_depth: int = 0,
        policy: HealthPolicy | None = None,
    ) -> tuple[HealthState, str]:
        p = policy or self.default_policy

        if not agent_health_states:
            return HealthState.UNKNOWN, "No registered agents"

        unhealthy_count = sum(1 for s in agent_health_states if s == HealthState.UNHEALTHY)
        degraded_count = sum(1 for s in agent_health_states if s == HealthState.DEGRADED)
        total = len(agent_health_states)

        if queue_depth > p.queue_depth_threshold:
            return HealthState.DEGRADED, f"Queue depth saturated ({queue_depth})"

        if unhealthy_count / total >= 0.5:
            return HealthState.UNHEALTHY, f"Majority of fleet unhealthy ({unhealthy_count}/{total})"
        if (unhealthy_count + degraded_count) / total >= 0.3:
            return (
                HealthState.DEGRADED,
                f"Significant degradation ({unhealthy_count + degraded_count}/{total})",
            )

        return HealthState.HEALTHY, "Fleet operational capacity nominal"


class SLOEngine:
    """Evaluates service level objectives, error budgets, and breach detections."""

    def __init__(self) -> None:
        self._definitions: dict[str, SLODefinition] = {}
        self._windows: dict[
            str, list[tuple[float, float]]
        ] = {}  # slo_id -> list of (timestamp, value)

    def register_slo(self, slo: SLODefinition) -> None:
        self._definitions[slo.slo_id] = slo
        if slo.slo_id not in self._windows:
            self._windows[slo.slo_id] = []

    def get_slo(self, slo_id: str) -> SLODefinition | None:
        return self._definitions.get(slo_id)

    def record_sample(self, slo_id: str, value: float, timestamp: float | None = None) -> None:
        ts = timestamp if timestamp is not None else time.time()
        if slo_id in self._windows:
            self._windows[slo_id].append((ts, value))

    def evaluate_slo(self, slo_id: str, now: float | None = None) -> SLOEvaluation:
        slo = self._definitions.get(slo_id)
        if not slo:
            raise KeyError(f"SLO '{slo_id}' not registered")

        t = now if now is not None else time.time()
        window_start = t - slo.window_s

        # Filter samples within window
        samples = [val for ts, val in self._windows[slo_id] if ts >= window_start]
        if not samples:
            return SLOEvaluation(
                slo_id=slo_id,
                sli_value=0.0,
                slo_target=slo.target,
                error_budget_remaining=1.0,
                breach=False,
                evaluated_at=t,
            )

        # Handle different metrics:
        # For latencies: smaller is better (p95 or average <= target)
        if "latency" in slo.metric or "wait" in slo.metric:
            sli = sum(samples) / len(samples)
            breach = sli > slo.target
            # Error budget: fraction of threshold not exceeded
            if slo.target > 0:
                ratio = max(0.0, 1.0 - (sli / slo.target))
                error_budget = ratio if not breach else 0.0
            else:
                error_budget = 0.0 if breach else 1.0
        else:
            # For availability / success rate: higher is better (sli >= target)
            sli = sum(samples) / len(samples)
            breach = sli < slo.target
            target_failure_allowance = max(1e-6, 1.0 - slo.target)
            actual_failure = max(0.0, 1.0 - sli)
            error_budget = max(0.0, 1.0 - (actual_failure / target_failure_allowance))

        return SLOEvaluation(
            slo_id=slo_id,
            sli_value=round(sli, 4),
            slo_target=slo.target,
            error_budget_remaining=round(error_budget, 4),
            breach=breach,
            evaluated_at=t,
        )

    def evaluate_all(self, now: float | None = None) -> list[SLOEvaluation]:
        return [self.evaluate_slo(slo_id, now=now) for slo_id in self._definitions]
