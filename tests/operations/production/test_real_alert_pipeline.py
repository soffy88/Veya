"""Production Qualification: Wave PQD Real Alert Pipeline & Storm Suppression (spec §22, §23).

Validates:
  Signal evaluation against registered rules.
  Deduplication of repeated alert bursts into single AlertInstance with occurrence counter.
  Zero duplicate alert instance creation (DUPLICATE_ALERT_INSTANCE=0).
  Alert storm suppression (ALERT_STORM=0).
  Full alert lifecycle: OPEN -> ACKNOWLEDGED -> RESOLVED.
  Raw events preserved in telemetry stream.
  REAL_ALERT_PIPELINE=PASS
"""

from __future__ import annotations

import time

from veya.operations import (
    AlertManager,
    AlertRule,
    AlertState,
)


def test_real_alert_pipeline_deduplication_and_lifecycle() -> None:
    mgr = AlertManager()

    # 1. Register alert rule
    rule = AlertRule(
        rule_id="rule_high_mem",
        name="High Memory Usage",
        severity="CRITICAL",
        metric="memory_pct",
        threshold=85.0,
        dedupe_key_template="{rule_id}:{target}",
    )
    mgr.register_rule(rule)

    now = time.time()

    # 2. Below threshold -> No alert created, raw event recorded
    alert_none = mgr.evaluate_signal(
        rule_id="rule_high_mem",
        target="agent_alpha",
        value=70.0,
        message="Memory at 70%",
        now=now,
    )
    assert alert_none is None
    assert len(mgr._raw_events) == 1

    # 3. First breach -> Creates OPEN AlertInstance
    alert1 = mgr.evaluate_signal(
        rule_id="rule_high_mem",
        target="agent_alpha",
        value=90.0,
        message="Memory spike 90%",
        details={"process": "runtime_worker"},
        now=now,
    )
    assert alert1 is not None
    assert alert1.state == AlertState.OPEN
    assert alert1.occurrence_count == 1
    assert alert1.severity == "CRITICAL"
    assert alert1.target == "agent_alpha"

    # 4. Burst of 50 signals for same rule and target -> ALERT_STORM=0, DUPLICATE_ALERT_INSTANCE=0
    for i in range(50):
        alert_burst = mgr.evaluate_signal(
            rule_id="rule_high_mem",
            target="agent_alpha",
            value=92.0 + (i * 0.1),
            message=f"Memory spike continuation {i}",
            now=now + (i * 0.1),
        )
        assert alert_burst.alert_id == alert1.alert_id  # Same instance!

    # Active alerts list has exactly 1 alert
    active = mgr.list_alerts(AlertState.OPEN)
    assert len(active) == 1
    assert active[0].occurrence_count == 51

    # Raw events preserved for forensic traceability
    assert len(mgr._raw_events) == 52

    # 5. Acknowledge alert
    ack = mgr.acknowledge_alert(alert1.alert_id, actor="ops_oncall")
    assert ack is not None
    assert ack.state == AlertState.ACKNOWLEDGED
    assert ack.details.get("acknowledged_by") == "ops_oncall"

    # 6. Resolve alert
    res = mgr.resolve_alert(alert1.alert_id, actor="ops_oncall")
    assert res is not None
    assert res.state == AlertState.RESOLVED
    assert res.details.get("resolved_by") == "ops_oncall"

    # Active alerts now empty
    assert len(mgr.list_alerts(AlertState.OPEN)) == 0
    assert len(mgr.list_alerts(AlertState.RESOLVED)) == 1
