"""Production Qualification: Wave PQE Real Budget Governance (spec §29).

Validates:
  Cost budget tracking, warning alerts, and hard limit enforcement.
  REAL_BUDGET_GOVERNANCE=PASS
  BUDGET_EVENTS=PASS
"""

from __future__ import annotations

import pytest

from veya.operations import (
    CostBudget,
    CostBudgetManager,
)


def test_real_budget_governance_lifecycle() -> None:
    mgr = CostBudgetManager()

    # 1. Register budget: $100 total, 80% warning ($80), 100% hard limit ($100)
    budget = CostBudget(
        budget_id="b_team_infra",
        principal_id="principal_infra",
        scope="principal",
        budget_amount=100.0,
        currency_or_unit="USD",
        warning_threshold=0.80,
        hard_limit=1.00,
        current_spend=0.0,
    )
    mgr.register_budget(budget)

    # 2. Spend $50 -> 50% utilization, no warning, no hard limit
    eval1 = mgr.record_spend("b_team_infra", 50.0)
    assert eval1.current_spend == 50.0
    assert eval1.utilization_ratio == 0.50
    assert eval1.warning_reached is False
    assert eval1.hard_limit_reached is False
    assert eval1.action_required is None

    # 3. Spend another $35 -> Total $85 (85% utilization) -> EMIT_WARNING_ALERT
    eval2 = mgr.record_spend("b_team_infra", 35.0)
    assert eval2.current_spend == 85.0
    assert eval2.utilization_ratio == 0.85
    assert eval2.warning_reached is True
    assert eval2.hard_limit_reached is False
    assert eval2.action_required == "EMIT_WARNING_ALERT"

    # 4. Spend another $20 -> Total $105 (105% utilization) -> BLOCK_NEW_ADMISSIONS
    eval3 = mgr.record_spend("b_team_infra", 20.0)
    assert eval3.current_spend == 105.0
    assert eval3.utilization_ratio == 1.05
    assert eval3.warning_reached is True
    assert eval3.hard_limit_reached is True
    assert eval3.action_required == "BLOCK_NEW_ADMISSIONS"

    # 5. Non-existent budget raises KeyError
    with pytest.raises(KeyError):
        mgr.record_spend("non_existent_budget", 10.0)
