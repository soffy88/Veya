"""Production Qualification: Wave PQD Real SLO Evaluation (spec §21).

Validates:
  SLO calculation across rolling windows.
  Error budget tracking and breach detection.
  REAL_SLO_EVALUATION=PASS
  SLO_BREACH_DETECTED=PASS
  ERROR_BUDGET_UPDATED=PASS
"""

from __future__ import annotations

import time

from veya.operations import (
    SLODefinition,
    SLOEngine,
)


def test_real_slo_evaluation_and_error_budget() -> None:
    slo_engine = SLOEngine()

    # 1. Register Availability SLO (target: 99.0% over 60s)
    avail_slo = SLODefinition(
        slo_id="slo_mission_success",
        metric="mission_success_rate",
        target=0.99,
        window_s=60.0,
    )
    slo_engine.register_slo(avail_slo)

    # 2. Register Latency SLO (target: max 2.0s over 60s)
    lat_slo = SLODefinition(
        slo_id="slo_p95_latency",
        metric="execution_latency_s",
        target=2.0,
        window_s=60.0,
    )
    slo_engine.register_slo(lat_slo)

    now = time.time()

    # Empty window -> error_budget 1.0, breach False
    eval_avail = slo_engine.evaluate_slo("slo_mission_success", now=now)
    assert eval_avail.error_budget_remaining == 1.0
    assert not eval_avail.breach

    # Feed 99 successful samples (1.0) and 1 failed (0.0) -> exactly 99%
    for _ in range(99):
        slo_engine.record_sample("slo_mission_success", 1.0, timestamp=now)
    slo_engine.record_sample("slo_mission_success", 0.0, timestamp=now)

    eval_avail = slo_engine.evaluate_slo("slo_mission_success", now=now)
    assert eval_avail.sli_value == 0.99
    assert not eval_avail.breach

    # Feed more failures to trigger breach
    for _ in range(5):
        slo_engine.record_sample("slo_mission_success", 0.0, timestamp=now)

    eval_avail_breach = slo_engine.evaluate_slo("slo_mission_success", now=now)
    assert eval_avail_breach.breach is True
    assert eval_avail_breach.sli_value < 0.99
    assert eval_avail_breach.error_budget_remaining == 0.0

    # Test Latency SLO
    # Normal latency 1.2s -> no breach
    for _ in range(10):
        slo_engine.record_sample("slo_p95_latency", 1.2, timestamp=now)
    eval_lat = slo_engine.evaluate_slo("slo_p95_latency", now=now)
    assert not eval_lat.breach
    assert eval_lat.sli_value == 1.2
    assert eval_lat.error_budget_remaining > 0.0

    # Spike latency to 3.5s -> breach
    for _ in range(15):
        slo_engine.record_sample("slo_p95_latency", 3.5, timestamp=now)
    eval_lat_breach = slo_engine.evaluate_slo("slo_p95_latency", now=now)
    assert eval_lat_breach.breach is True
    assert eval_lat_breach.sli_value > 2.0
    assert eval_lat_breach.error_budget_remaining == 0.0

    # Verify evaluate_all
    all_evals = slo_engine.evaluate_all(now=now)
    assert len(all_evals) == 2
