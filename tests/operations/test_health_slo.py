"""Tests for Health and SLO Engine (Phase O2)."""

from __future__ import annotations

import time

from veya.operations.health_slo import HealthEvaluator, SLOEngine
from veya.operations.models import HealthState, SLODefinition


def test_health_evaluation_thresholds() -> None:
    evaluator = HealthEvaluator()

    # Nominal healthy agent
    state, _msg = evaluator.evaluate_agent_health(
        last_heartbeat_s_ago=5.0,
        error_rate=0.01,
        latency_s=1.0,
    )
    assert state == HealthState.HEALTHY

    # Delayed heartbeat -> DEGRADED
    state, _msg = evaluator.evaluate_agent_health(
        last_heartbeat_s_ago=35.0,  # > 30s threshold
        error_rate=0.01,
        latency_s=1.0,
    )
    assert state == HealthState.DEGRADED

    # High error rate -> UNHEALTHY
    state, _msg = evaluator.evaluate_agent_health(
        last_heartbeat_s_ago=5.0,
        error_rate=0.15,  # > 10%
        latency_s=1.0,
    )
    assert state == HealthState.UNHEALTHY

    # Fleet health evaluation
    fleet_state, _f_msg = evaluator.evaluate_fleet_health(
        [HealthState.HEALTHY, HealthState.HEALTHY, HealthState.UNHEALTHY],
        queue_depth=10,
    )
    assert fleet_state in (HealthState.HEALTHY, HealthState.DEGRADED)


def test_slo_evaluation_and_error_budget() -> None:
    engine = SLOEngine()

    # Availability SLO (target 99%)
    engine.register_slo(
        SLODefinition(
            slo_id="slo_avail",
            metric="availability",
            target=0.99,
            window_s=3600.0,
        )
    )

    now = time.time()
    # 99 successes, 1 failure -> 99.0%
    for _ in range(99):
        engine.record_sample("slo_avail", 1.0, timestamp=now)
    engine.record_sample("slo_avail", 0.0, timestamp=now)

    res = engine.evaluate_slo("slo_avail", now=now)
    assert res.sli_value == 0.99
    assert not res.breach
    assert res.error_budget_remaining >= 0.0

    # Latency SLO (target <= 2.0s)
    engine.register_slo(
        SLODefinition(
            slo_id="slo_latency",
            metric="admission_latency",
            target=2.0,
            window_s=3600.0,
        )
    )
    # Record fast latencies
    engine.record_sample("slo_latency", 1.0, timestamp=now)
    engine.record_sample("slo_latency", 1.5, timestamp=now)
    res_lat = engine.evaluate_slo("slo_latency", now=now)
    assert not res_lat.breach
    assert res_lat.sli_value == 1.25

    # Trigger breach
    engine.record_sample("slo_latency", 5.0, timestamp=now)
    res_breach = engine.evaluate_slo("slo_latency", now=now)
    assert res_breach.breach
    assert res_breach.error_budget_remaining == 0.0
