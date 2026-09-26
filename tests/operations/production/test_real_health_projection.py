"""Production Qualification: Wave PQD Real Health Projection (spec §20).

Validates:
  Health projection for agents and fleet based on real operational signals.
  HEALTHY, DEGRADED, UNHEALTHY, UNKNOWN transitions without state forgery.
  REAL_HEALTH_PROJECTION=PASS
"""

from __future__ import annotations

from veya.operations import (
    HealthEvaluator,
    HealthPolicy,
    HealthState,
)


def test_real_health_projection_agent_and_fleet() -> None:
    policy = HealthPolicy(
        policy_id="test_health_policy",
        heartbeat_timeout_s=30.0,
        error_rate_threshold=0.05,  # 5%
        latency_threshold_s=5.0,
        lease_failure_threshold=3,
        execution_failure_threshold=3,
        queue_depth_threshold=20,
    )
    evaluator = HealthEvaluator(default_policy=policy)

    # 1. Agent nominal: HEALTHY
    state, reason = evaluator.evaluate_agent_health(
        last_heartbeat_s_ago=5.0,
        error_rate=0.01,
        latency_s=1.2,
        lease_failures=0,
        execution_failures=0,
    )
    assert state == HealthState.HEALTHY
    assert "nominal" in reason.lower() or "within" in reason.lower()

    # 2. Delayed heartbeat (35s > 30s): DEGRADED
    state, reason = evaluator.evaluate_agent_health(
        last_heartbeat_s_ago=35.0,
        error_rate=0.01,
        latency_s=1.0,
    )
    assert state == HealthState.DEGRADED
    assert "Heartbeat delayed" in reason

    # 3. Heartbeat lost (65s > 2 * 30s): UNHEALTHY
    state, reason = evaluator.evaluate_agent_health(
        last_heartbeat_s_ago=65.0,
        error_rate=0.01,
        latency_s=1.0,
    )
    assert state == HealthState.UNHEALTHY
    assert "Heartbeat lost" in reason

    # 4. Excessive execution failures: UNHEALTHY
    state, reason = evaluator.evaluate_agent_health(
        last_heartbeat_s_ago=5.0,
        error_rate=0.02,
        latency_s=1.0,
        execution_failures=4,
    )
    assert state == HealthState.UNHEALTHY
    assert "Excessive execution failures" in reason

    # 5. Fleet evaluation
    # Empty fleet -> UNKNOWN
    fleet_state, msg = evaluator.evaluate_fleet_health([])
    assert fleet_state == HealthState.UNKNOWN

    # Majority healthy -> HEALTHY
    states = [HealthState.HEALTHY, HealthState.HEALTHY, HealthState.HEALTHY, HealthState.DEGRADED]
    fleet_state, msg = evaluator.evaluate_fleet_health(states, queue_depth=5)
    assert fleet_state == HealthState.HEALTHY

    # Queue saturation -> DEGRADED
    fleet_state, msg = evaluator.evaluate_fleet_health(states, queue_depth=25)
    assert fleet_state == HealthState.DEGRADED
    assert "Queue depth saturated" in msg

    # Majority unhealthy -> UNHEALTHY
    degraded_fleet = [HealthState.UNHEALTHY, HealthState.UNHEALTHY, HealthState.HEALTHY]
    fleet_state, msg = evaluator.evaluate_fleet_health(degraded_fleet)
    assert fleet_state == HealthState.UNHEALTHY
