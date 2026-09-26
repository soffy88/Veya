"""P4-B observability, SLO and incident-recovery proofs."""

import pytest
from pytest import approx

from runtime.execution.observability import (
    ExecutionMetrics,
    HealthReadiness,
    IncidentRecovery,
    SLOCalculator,
    TraceCorrelator,
)


def test_metrics_and_slos_are_computable():
    metrics = ExecutionMetrics(queue_depth=3, active_bots=2, active_runs=2)
    for index in range(20):
        metrics.record_goalrun(
            float(index), float(index) + index / 10, "success" if index else "failed"
        )
    metrics.record_recovery(True)
    metrics.record_recovery(False)
    metrics.record_failure(provider=True, tool=True)
    metrics.record_control_event("restart")
    metrics.record_control_event("replan")
    metrics.record_control_event("verifier")
    metrics.record_control_event("budget_exhausted")
    metrics.record_side_effect(duplicate=False)
    metrics.record_side_effect(duplicate=True)
    slos = SLOCalculator(metrics).calculate()

    assert metrics.snapshot()["queue_depth"] == 3
    assert slos["GOALRUN_SUCCESS_RATE"] == 19 / 20
    assert slos["P95_GOALRUN_LATENCY"] == approx(1.8)
    assert slos["RECOVERY_SUCCESS_RATE"] == 0.5
    assert slos["DUPLICATE_SIDE_EFFECT_RATE"] == 0.5
    assert slos["FALSE_SUCCESS_RATE"] == 0.0


@pytest.mark.asyncio
async def test_health_readiness_is_fail_closed():
    async def healthy():
        return {"ok": True}

    ready = await HealthReadiness.readiness(
        durable_backend=healthy,
        provider=healthy,
        supervisor=healthy,
    )
    assert ready["ok"]
    assert HealthReadiness.liveness()["ok"]

    degraded = await HealthReadiness.readiness(
        durable_backend=healthy,
        provider=lambda: {"ok": True, "degraded": True},
        supervisor=healthy,
    )
    assert degraded["status"] == "not_ready"

    broken = await HealthReadiness.readiness(
        durable_backend=lambda: (_ for _ in ()).throw(ConnectionError("store unavailable")),
        provider=healthy,
        supervisor=healthy,
    )
    assert not broken["ok"]
    assert broken["checks"]["durable_backend"]["ok"] is False


def test_trace_correlation_keeps_full_authority_chain():
    trace = TraceCorrelator()
    common = {"trace_id": "trace-1", "bot_id": "bot-a", "goal_run_id": "goal-1"}
    trace.record(**common, action_id="action-1", delegate_id="delegate-1", event="delegate.result")
    trace.record(**common, action_id="action-1", event="tool.result")
    trace.record(
        **common, action_id="action-1", verifier_id="verifier-1", event="verification.PASS"
    )
    chain = trace.trace("trace-1")
    assert trace.is_complete("trace-1")
    assert {item["goal_run_id"] for item in chain} == {"goal-1"}
    assert chain[0]["delegate_id"] == "delegate-1"
    assert chain[-1]["verifier_id"] == "verifier-1"


@pytest.mark.asyncio
async def test_incident_recovery_covers_fail_closed_incident_classes():
    metrics = ExecutionMetrics()
    incidents = []
    recovery = IncidentRecovery(metrics, on_event=incidents.append)
    classes = ("provider", "tool", "worker", "supervisor", "durable-store")

    for incident in classes:
        attempts = 0

        async def operation(incident_name=incident):
            nonlocal attempts
            attempts += 1
            raise OSError(incident_name)

        async def recover(incident_name=incident):
            return {"status": "resumed", "incident": incident_name}

        result = await recovery.run(incident, operation, recover)
        assert result["status"] == "resumed"
        assert attempts == 1

    assert set(recovery.incidents) == set(classes)
    assert metrics.recovery_total == 5
    assert metrics.recovery_success == 5
    assert len([event for event in incidents if event["event"] == "recovery.completed"]) == 5

    async def failed_recovery():
        raise ConnectionError("still down")

    with pytest.raises(RuntimeError, match="remains blocked"):
        await recovery.run("durable-store", operation, failed_recovery)
    assert metrics.recovery_success == 5
