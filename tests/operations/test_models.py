"""Tests for Operations V1 Core Models (Phase O1)."""

from __future__ import annotations

import time

from veya.operations.models import (
    AgentOperationalState,
    AgentOperationalStatus,
    AlertInstance,
    AlertRule,
    AlertState,
    CostBudget,
    HealthPolicy,
    IncidentStatus,
    MaintenanceScope,
    MaintenanceWindow,
    OperationalAuditRecord,
    OperationalIncident,
    OperationalPolicy,
    QuotaEnforcementAction,
    QuotaPolicy,
    RolloutPlan,
    RolloutRevision,
    RolloutStatus,
    RolloutTarget,
    SLODefinition,
    SLOEvaluation,
    UsageRecord,
)


def test_o1_models_instantiation_and_serialization() -> None:
    # 1. OperationalPolicy
    policy = OperationalPolicy(
        policy_id="pol_1",
        name="StrictProductionPolicy",
        rules={"max_unhealthy_ratio": 0.2},
    )
    assert policy.to_dict()["policy_id"] == "pol_1"
    restored_pol = OperationalPolicy.from_dict(policy.to_dict())
    assert restored_pol.name == "StrictProductionPolicy"

    # 2. AgentOperationalState
    agent_state = AgentOperationalState(
        agent_id="agent_alpha",
        desired_state=AgentOperationalStatus.PAUSED,
        observed_state=AgentOperationalStatus.ACTIVE,
        reason="Scheduled maintenance pause",
    )
    d = agent_state.to_dict()
    assert d["desired_state"] == "PAUSED"
    assert d["observed_state"] == "ACTIVE"
    restored_agent = AgentOperationalState.from_dict(d)
    assert restored_agent.desired_state == AgentOperationalStatus.PAUSED

    # 3. MaintenanceWindow
    now = time.time()
    mw = MaintenanceWindow(
        id="mw_1",
        scope=MaintenanceScope.AGENT,
        target_id="agent_alpha",
        starts_at=now - 10,
        ends_at=now + 3600,
        reason="OS patch upgrade",
        created_by="operator_ops",
    )
    assert mw.is_active(now)
    assert not mw.is_active(now + 4000)
    assert mw.to_dict()["scope"] == "agent"

    # 4. RolloutPlan
    rollout = RolloutPlan(
        rollout_id="ro_1",
        artifact_version="v1.2.0",
        target_selector={"zone": "us-east"},
        batch_size=2,
        targets=[
            RolloutTarget(agent_id="agent_1", current_version="v1.1.0", target_version="v1.2.0")
        ],
        rollback_revision=RolloutRevision("rev_0", "v1.1.0", 1),
    )
    assert rollout.status == RolloutStatus.PENDING
    d_ro = rollout.to_dict()
    restored_ro = RolloutPlan.from_dict(d_ro)
    assert restored_ro.artifact_version == "v1.2.0"

    # 5. HealthPolicy & SLO
    hp = HealthPolicy(policy_id="hp_1", error_rate_threshold=0.02)
    assert hp.error_rate_threshold == 0.02

    SLODefinition(slo_id="slo_latency", metric="admission_latency", target=1.5)
    eval_res = SLOEvaluation(
        slo_id="slo_latency",
        sli_value=1.2,
        slo_target=1.5,
        error_budget_remaining=0.85,
        breach=False,
    )
    assert not eval_res.breach

    # 6. Alert & Incident
    rule = AlertRule(rule_id="r_crash", name="AgentCrashRule", metric="crash_count", threshold=1.0)
    alert = AlertInstance(
        alert_id="alt_1",
        rule_id=rule.rule_id,
        dedupe_key="r_crash:agent_alpha",
        severity="CRITICAL",
        target="agent_alpha",
        message="Agent crashed unexpectedly",
    )
    assert alert.state == AlertState.OPEN
    d_alt = alert.to_dict()
    assert d_alt["state"] == "OPEN"

    incident = OperationalIncident(
        incident_id="inc_1",
        severity="HIGH",
        scope="agent_alpha",
        root_cause="Host OOM",
        affected_agents=["agent_alpha"],
    )
    assert incident.status == IncidentStatus.OPEN

    # 7. Quota & Cost
    quota = QuotaPolicy(
        policy_id="q_1",
        principal_id="user_prod",
        dimensions={"concurrent_missions": 5.0},
        enforcement_action=QuotaEnforcementAction.BLOCK,
    )
    assert quota.enforcement_action == QuotaEnforcementAction.BLOCK

    usage = UsageRecord(
        record_id="rec_1",
        principal_id="user_prod",
        agent_id="agent_alpha",
        mission_id="m_1",
        goal_run_id="gr_1",
        execution_id="ex_1",
        provider="anthropic",
        model="claude-3-5-sonnet",
        resource_type="token",
        quantity=1500.0,
        unit="tokens",
    )
    assert usage.quantity == 1500.0

    budget = CostBudget(
        budget_id="b_1",
        principal_id="user_prod",
        budget_amount=50.0,
        warning_threshold=0.8,
        hard_limit=1.0,
    )
    assert budget.budget_amount == 50.0

    # 8. Audit Record
    audit = OperationalAuditRecord(
        actor="ops_admin",
        action="pause",
        target="agent_alpha",
        reason="Emergency maintenance",
    )
    assert audit.action == "pause"
    assert audit.record_id.startswith("audit_")
