"""Tests for Operational Alerting and Incident Correlation (Phase O4)."""

from __future__ import annotations

from veya.operations.alerts import AlertManager
from veya.operations.models import AlertRule, AlertState, IncidentStatus


def test_alert_evaluation_deduplication_and_lifecycle() -> None:
    mgr = AlertManager()
    mgr.register_rule(
        AlertRule(
            rule_id="r_agent_crash",
            name="AgentCrashRule",
            metric="crash_count",
            threshold=1.0,
            severity="CRITICAL",
        )
    )

    # 1. Fire signal breaching threshold
    alt1 = mgr.evaluate_signal(
        rule_id="r_agent_crash",
        target="agent_x",
        value=1.0,
        message="Agent crashed",
    )
    assert alt1 is not None
    assert alt1.occurrence_count == 1
    assert alt1.state == AlertState.OPEN

    # 2. Fire 100 more signals on same target -> verify deduplication (ALERT_STORM=0)
    for _ in range(100):
        alt_repeated = mgr.evaluate_signal(
            rule_id="r_agent_crash",
            target="agent_x",
            value=1.0,
            message="Agent crashed",
        )
        assert alt_repeated.alert_id == alt1.alert_id

    assert alt1.occurrence_count == 101
    assert len(mgr.list_alerts()) == 1  # DUPLICATE_ALERT_INSTANCE=0

    # 3. Acknowledge
    ack = mgr.acknowledge_alert(alt1.alert_id, actor="ops_oncall")
    assert ack is not None
    assert ack.state == AlertState.ACKNOWLEDGED

    # 4. Resolve
    res = mgr.resolve_alert(alt1.alert_id, actor="ops_oncall")
    assert res is not None
    assert res.state == AlertState.RESOLVED
    assert len(mgr.list_alerts(state=AlertState.OPEN)) == 0


def test_incident_correlation_and_raw_events() -> None:
    mgr = AlertManager()
    mgr.register_rule(
        AlertRule(rule_id="r_oom", name="OOMRule", metric="oom_events", threshold=1.0)
    )

    # Fire events
    mgr.evaluate_signal("r_oom", "agent_node_1", 1.0, "Out of memory on node")
    mgr.evaluate_signal("r_oom", "agent_node_1", 2.0, "Repeated OOM")

    # Correlate into incident
    inc = mgr.create_incident(
        severity="HIGH",
        scope="node_cluster",
        root_cause="Host kernel OOM-killer invoked",
        affected_agents=["agent_node_1"],
        affected_missions=["m_101", "m_102"],
    )
    assert inc.status == IncidentStatus.OPEN
    assert len(inc.events) >= 2  # RAW_EVENT_PRESERVED=YES

    # Resolve incident
    res_inc = mgr.resolve_incident(inc.incident_id, resolution_notes="Host RAM increased")
    assert res_inc is not None
    assert res_inc.status == IncidentStatus.RESOLVED
