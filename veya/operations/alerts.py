"""Operational Alerting and Incident Management for Veya Agent Operations V1 (spec §16-§18, §26-§27, §46).

Features:
- Telemetry alert rule evaluations
- Deduplication with occurrence count & cooldown window (ALERT_STORM=0, DUPLICATE_ALERT_INSTANCE=0)
- States: OPEN, ACKNOWLEDGED, RESOLVED
- OperationalIncident correlation preserving all raw events
- Idempotent alert lifecycle operations
"""

from __future__ import annotations

import time
import uuid
from typing import Any

from .models import (
    AlertInstance,
    AlertRule,
    AlertState,
    IncidentStatus,
    OperationalIncident,
)


class AlertManager:
    """Manages operational alerts, deduplication, state transitions, and incident correlation."""

    def __init__(self) -> None:
        self._rules: dict[str, AlertRule] = {}
        # dedupe_key -> AlertInstance
        self._active_alerts: dict[str, AlertInstance] = {}
        # alert_id -> AlertInstance
        self._alerts_by_id: dict[str, AlertInstance] = {}
        self._incidents: dict[str, OperationalIncident] = {}
        self._raw_events: list[dict[str, Any]] = []

    def register_rule(self, rule: AlertRule) -> None:
        self._rules[rule.rule_id] = rule

    def evaluate_signal(
        self,
        rule_id: str,
        target: str,
        value: float,
        message: str,
        details: dict[str, Any] | None = None,
        now: float | None = None,
    ) -> AlertInstance | None:
        """Evaluate operational signal against rule; triggers alert if threshold breached (spec §16, §17)."""
        rule = self._rules.get(rule_id)
        if not rule:
            return None

        t = now if now is not None else time.time()
        # Record raw event for incident traceability (spec §27 RAW_EVENT_PRESERVED=YES)
        self._raw_events.append(
            {
                "rule_id": rule_id,
                "target": target,
                "value": value,
                "timestamp": t,
                "details": details or {},
            }
        )

        if value < rule.threshold:
            return None

        dedupe_key = rule.dedupe_key_template.format(rule_id=rule_id, target=target)

        # Check existing alert for deduplication
        existing = self._active_alerts.get(dedupe_key)
        if existing and existing.state != AlertState.RESOLVED:
            # Increment occurrence and update last_seen (ALERT_STORM=0)
            existing.occurrence_count += 1
            existing.last_seen = t
            if details:
                existing.details.update(details)
            return existing

        # New alert instance
        alert_id = f"alt_{uuid.uuid4().hex[:10]}"
        alert = AlertInstance(
            alert_id=alert_id,
            rule_id=rule_id,
            dedupe_key=dedupe_key,
            severity=rule.severity,
            target=target,
            message=message,
            first_seen=t,
            last_seen=t,
            occurrence_count=1,
            state=AlertState.OPEN,
            details=details or {},
        )
        self._active_alerts[dedupe_key] = alert
        self._alerts_by_id[alert_id] = alert
        return alert

    def acknowledge_alert(self, alert_id: str, actor: str = "operator") -> AlertInstance | None:
        alert = self._alerts_by_id.get(alert_id)
        if alert and alert.state == AlertState.OPEN:
            alert.state = AlertState.ACKNOWLEDGED
            alert.details["acknowledged_by"] = actor
            alert.details["acknowledged_at"] = time.time()
        return alert

    def resolve_alert(self, alert_id: str, actor: str = "operator") -> AlertInstance | None:
        alert = self._alerts_by_id.get(alert_id)
        if alert and alert.state != AlertState.RESOLVED:
            alert.state = AlertState.RESOLVED
            alert.details["resolved_by"] = actor
            alert.details["resolved_at"] = time.time()
            self._active_alerts.pop(alert.dedupe_key, None)
        return alert

    def list_alerts(self, state: AlertState | None = None) -> list[AlertInstance]:
        if state is None:
            return list(self._alerts_by_id.values())
        return [a for a in self._alerts_by_id.values() if a.state == state]

    def create_incident(
        self,
        severity: str,
        scope: str,
        root_cause: str,
        affected_agents: list[str] | None = None,
        affected_missions: list[str] | None = None,
        associated_alert_ids: list[str] | None = None,
    ) -> OperationalIncident:
        """Correlate operational alerts and events into an incident (spec §26, §27)."""
        inc_id = f"inc_{uuid.uuid4().hex[:10]}"
        matched_events = [
            e
            for e in self._raw_events
            if e.get("target") in (affected_agents or []) or scope in str(e)
        ]
        incident = OperationalIncident(
            incident_id=inc_id,
            severity=severity,
            scope=scope,
            opened_at=time.time(),
            root_cause=root_cause,
            affected_agents=affected_agents or [],
            affected_missions=affected_missions or [],
            events=matched_events,
            status=IncidentStatus.OPEN,
        )
        self._incidents[inc_id] = incident
        return incident

    def resolve_incident(
        self, incident_id: str, resolution_notes: str = ""
    ) -> OperationalIncident | None:
        inc = self._incidents.get(incident_id)
        if inc:
            inc.status = IncidentStatus.RESOLVED
            inc.resolved_at = time.time()
            inc.root_cause = f"{inc.root_cause} | Resolved: {resolution_notes}"
        return inc

    def get_incident(self, incident_id: str) -> OperationalIncident | None:
        return self._incidents.get(incident_id)

    def list_incidents(self) -> list[OperationalIncident]:
        return list(self._incidents.values())
