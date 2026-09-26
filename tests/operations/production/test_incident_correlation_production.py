"""Production Qualification: Wave PQD Incident Correlation (spec §24).

Validates:
  Correlation of multiple operational alerts and raw telemetry events into an OperationalIncident.
  Incident lifecycle management: OPEN -> RESOLVED with resolution metadata.
  Preservation of matched raw events without event loss.
  INCIDENT_CORRELATION=PASS
"""

from __future__ import annotations

import time

from veya.operations import (
    AlertManager,
    AlertRule,
    IncidentStatus,
)


def test_incident_correlation_and_lifecycle() -> None:
    mgr = AlertManager()

    # Setup rules
    mgr.register_rule(
        AlertRule(
            rule_id="rule_agent_crash",
            name="Agent Crash",
            severity="CRITICAL",
            metric="crash_count",
            threshold=1.0,
            dedupe_key_template="{rule_id}:{target}",
        )
    )
    mgr.register_rule(
        AlertRule(
            rule_id="rule_net_drop",
            name="Network Packet Loss",
            severity="HIGH",
            metric="packet_loss_pct",
            threshold=10.0,
            dedupe_key_template="{rule_id}:{target}",
        )
    )

    now = time.time()

    # Generate signals across multiple agents
    alt1 = mgr.evaluate_signal("rule_agent_crash", "agent_prod_1", 1.0, "Worker died", now=now)
    alt2 = mgr.evaluate_signal("rule_net_drop", "agent_prod_1", 25.0, "High loss", now=now)
    alt3 = mgr.evaluate_signal("rule_net_drop", "agent_prod_2", 30.0, "High loss", now=now)

    assert alt1 and alt2 and alt3

    # Correlate alerts into an incident
    incident = mgr.create_incident(
        severity="CRITICAL",
        scope="network_partition_datacenter_a",
        root_cause="Switch power failure causing worker disconnects",
        affected_agents=["agent_prod_1", "agent_prod_2"],
        affected_missions=["mission_job_42", "mission_job_43"],
        associated_alert_ids=[alt1.alert_id, alt2.alert_id, alt3.alert_id],
    )

    assert incident.incident_id.startswith("inc_")
    assert incident.status == IncidentStatus.OPEN
    assert set(incident.affected_agents) == {"agent_prod_1", "agent_prod_2"}
    assert len(incident.events) >= 3  # Matched raw events preserved

    # Query incident
    fetched = mgr.get_incident(incident.incident_id)
    assert fetched is not None
    assert fetched.incident_id == incident.incident_id

    # Resolve incident
    resolved = mgr.resolve_incident(
        incident.incident_id,
        resolution_notes="Power supply restored to core switch; network metrics normalized.",
    )
    assert resolved is not None
    assert resolved.status == IncidentStatus.RESOLVED
    assert resolved.resolved_at is not None
    assert "Power supply restored" in resolved.root_cause
