"""Tests for Quota, UsageLedger, and Cost Governance (Phase O5)."""

from __future__ import annotations

import pytest

from veya.operations.models import (
    CostBudget,
    ProviderOperationalStatus,
    QuotaEnforcementAction,
    QuotaPolicy,
    UsageRecord,
)
from veya.operations.quota_cost import (
    CostBudgetManager,
    ProviderOperationalManager,
    QuotaExceededError,
    QuotaManager,
    UsageLedgerManager,
)


def test_quota_enforcement_and_accounting() -> None:
    mgr = QuotaManager()
    policy = QuotaPolicy(
        policy_id="qp_dev",
        principal_id="dev_team",
        dimensions={"concurrent_missions": 2.0},
        enforcement_action=QuotaEnforcementAction.BLOCK,
    )
    mgr.register_policy(policy)

    # Acquire 1
    assert mgr.check_and_acquire("dev_team", "concurrent_missions", 1.0)
    assert mgr.get_usage("dev_team", "concurrent_missions") == 1.0

    # Acquire 2nd
    assert mgr.check_and_acquire("dev_team", "concurrent_missions", 1.0)

    # 3rd acquisition must be BLOCKED
    with pytest.raises(QuotaExceededError) as exc_info:
        mgr.check_and_acquire("dev_team", "concurrent_missions", 1.0)
    assert exc_info.value.dimension == "concurrent_missions"
    assert exc_info.value.action == QuotaEnforcementAction.BLOCK

    # Release 1 -> can acquire again
    mgr.release("dev_team", "concurrent_missions", 1.0)
    assert mgr.check_and_acquire("dev_team", "concurrent_missions", 1.0)


def test_usage_ledger_idempotency() -> None:
    ledger = UsageLedgerManager()
    rec1 = UsageRecord(
        record_id="rec_100",
        principal_id="u1",
        agent_id="a1",
        mission_id="m1",
        goal_run_id="gr1",
        execution_id="ex1",
        provider="openai",
        model="gpt-4o",
        resource_type="token",
        quantity=500.0,
        unit="tokens",
        source_event_id="evt_unique_100",
    )

    # 1. First record succeeds
    assert ledger.record_usage(rec1)

    # 2. Duplicate record with same record_id is ignored (DUPLICATE_USAGE_CHARGE=0)
    assert not ledger.record_usage(rec1)

    # 3. Duplicate record with different record_id but same source_event_id is ignored
    rec2 = UsageRecord(
        record_id="rec_101",
        principal_id="u1",
        agent_id="a1",
        mission_id="m1",
        goal_run_id="gr1",
        execution_id="ex1",
        provider="openai",
        model="gpt-4o",
        resource_type="token",
        quantity=500.0,
        unit="tokens",
        source_event_id="evt_unique_100",
    )
    assert not ledger.record_usage(rec2)
    assert ledger.total_quantity("u1", "token") == 500.0


def test_cost_budget_thresholds() -> None:
    bm = CostBudgetManager()
    b = CostBudget(
        budget_id="b_team",
        principal_id="team_a",
        budget_amount=100.0,
        warning_threshold=0.8,
        hard_limit=1.0,
    )
    bm.register_budget(b)

    # Spend 50 -> nominal
    eval1 = bm.record_spend("b_team", 50.0)
    assert not eval1.warning_reached
    assert not eval1.hard_limit_reached
    assert eval1.action_required is None

    # Spend 35 (total 85) -> warning reached
    eval2 = bm.record_spend("b_team", 35.0)
    assert eval2.warning_reached
    assert not eval2.hard_limit_reached
    assert eval2.action_required == "EMIT_WARNING_ALERT"

    # Spend 20 (total 105) -> hard limit reached
    eval3 = bm.record_spend("b_team", 20.0)
    assert eval3.hard_limit_reached
    assert eval3.action_required == "BLOCK_NEW_ADMISSIONS"


def test_provider_cooldown_operational_projection() -> None:
    pm = ProviderOperationalManager()
    assert pm.get_status("anthropic") == ProviderOperationalStatus.AVAILABLE

    # Cooldown on quota exhaustion
    pm.mark_cooldown("anthropic", duration_s=100.0)
    assert pm.get_status("anthropic") == ProviderOperationalStatus.COOLDOWN
    assert pm.get_retry_not_before("anthropic") is not None
