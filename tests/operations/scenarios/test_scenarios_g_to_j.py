"""Qualification Scenarios G, H, I, J for Operations V1 (spec §56-§59).

Scenario G: SLO Breach (detection, error budget, alert creation, zero semantic tampering)
Scenario H: Alert Storm (>=100 signals deduplicated into 1 alert instance)
Scenario I: Quota Enforcement (block, no over-quota admission)
Scenario J: Cost Budget (warning threshold, hard limit, zero silent downgrade)
"""

from __future__ import annotations

import tempfile
import time

import pytest

from veya.operations import (
    AlertRule,
    AlertState,
    CostBudget,
    OperationsController,
    QuotaEnforcementAction,
    QuotaExceededError,
    QuotaPolicy,
    SLODefinition,
)


def test_scenario_g_slo_breach() -> None:
    """Scenario G: Queue latency breach triggers SLO breach, error budget exhaustion, and alert (spec §56)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        ops = OperationsController(base_dir=tmpdir)

        # Register SLO: queue_wait <= 1.0s
        ops.slo.register_slo(
            SLODefinition(
                slo_id="slo_queue_wait",
                metric="queue_wait",
                target=1.0,
                window_s=600.0,
            )
        )
        # Register corresponding alert rule
        ops.alerts.register_rule(
            AlertRule(
                rule_id="r_slo_queue",
                name="QueueWaitBreach",
                metric="queue_wait",
                threshold=1.0,
                severity="WARN",
            )
        )

        # Feed latency breach samples
        now = time.time()
        for _ in range(5):
            ops.slo.record_sample("slo_queue_wait", 3.5, timestamp=now)

        eval_res = ops.slo.evaluate_slo("slo_queue_wait", now=now)
        assert eval_res.breach  # SLO_BREACH_DETECTED=YES
        assert eval_res.error_budget_remaining == 0.0  # ERROR_BUDGET_UPDATED=YES

        # Fire alert on breach
        alt = ops.alerts.evaluate_signal(
            rule_id="r_slo_queue",
            target="fleet_queue",
            value=eval_res.sli_value,
            message="Queue wait exceeded 1.0s SLO target",
            now=now,
        )
        assert alt is not None
        assert alt.state == AlertState.OPEN  # ALERT_CREATED=YES


def test_scenario_h_alert_storm() -> None:
    """Scenario H: >= 100 identical failure signals collapse into exactly 1 alert instance (spec §57)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        ops = OperationsController(base_dir=tmpdir)
        ops.alerts.register_rule(
            AlertRule(
                rule_id="r_storm_test",
                name="NetworkFlap",
                metric="dropped_packets",
                threshold=10.0,
                severity="CRITICAL",
            )
        )

        # Send 120 signals
        for _i in range(120):
            ops.alerts.evaluate_signal(
                rule_id="r_storm_test",
                target="gateway_router_1",
                value=50.0,
                message="High packet drop",
            )

        open_alerts = ops.alerts.list_alerts(state=AlertState.OPEN)
        assert len(open_alerts) == 1  # ALERT_INSTANCE_COUNT=1, ALERT_STORM=0
        assert open_alerts[0].occurrence_count == 120


def test_scenario_i_quota() -> None:
    """Scenario I: Exceeding principal concurrent mission quota is strictly blocked (spec §58)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        ops = OperationsController(base_dir=tmpdir)
        ops.quota.register_policy(
            QuotaPolicy(
                policy_id="qp_tenant",
                principal_id="tenant_gamma",
                dimensions={"concurrent_missions": 3.0},
                enforcement_action=QuotaEnforcementAction.BLOCK,
            )
        )

        # Acquire up to limit
        for _ in range(3):
            assert ops.quota.check_and_acquire("tenant_gamma", "concurrent_missions", 1.0)

        # 4th must fail with QuotaExceededError
        with pytest.raises(QuotaExceededError) as exc_info:
            ops.quota.check_and_acquire("tenant_gamma", "concurrent_missions", 1.0)
        assert exc_info.value.limit == 3.0
        assert exc_info.value.requested == 4.0
        # OVER_QUOTA_ADMISSION=0, FALSE_SUCCESS=0


def test_scenario_j_budget() -> None:
    """Scenario J: Warning and hard limit reached with zero silent model downgrade (spec §59)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        ops = OperationsController(base_dir=tmpdir)
        ops.budgets.register_budget(
            CostBudget(
                budget_id="b_scen_j",
                principal_id="team_j",
                budget_amount=200.0,
                warning_threshold=0.8,
                hard_limit=1.0,
            )
        )

        # Spend 165 (82.5%) -> warning triggered
        eval_warn = ops.budgets.record_spend("b_scen_j", 165.0)
        assert eval_warn.warning_reached  # BUDGET_WARNING=PASS
        assert not eval_warn.hard_limit_reached
        assert eval_warn.action_required == "EMIT_WARNING_ALERT"

        # Spend additional 40 (total 205, >100%) -> hard limit triggered
        eval_hard = ops.budgets.record_spend("b_scen_j", 40.0)
        assert eval_hard.hard_limit_reached  # BUDGET_HARD_LIMIT=PASS
        assert eval_hard.action_required == "BLOCK_NEW_ADMISSIONS"
