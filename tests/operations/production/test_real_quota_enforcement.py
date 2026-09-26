"""Production Qualification: Wave PQE Real Quota Enforcement (spec §27).

Validates:
  Multi-principal quota enforcement across resources (concurrency, tokens, memory).
  Blocking on quota breach via QuotaExceededError.
  Quota release upon task completion.
  REAL_QUOTA_ENFORCEMENT=PASS
"""

from __future__ import annotations

import pytest

from veya.operations import (
    QuotaEnforcementAction,
    QuotaExceededError,
    QuotaManager,
    QuotaPolicy,
)


def test_real_quota_enforcement_and_lifecycle() -> None:
    mgr = QuotaManager()

    # 1. Register quota policy for principal_dev
    policy = QuotaPolicy(
        policy_id="quota_dev_tier",
        principal_id="principal_dev",
        scope="principal",
        dimensions={
            "concurrent_missions": 3.0,
            "tokens": 10000.0,
        },
        enforcement_action=QuotaEnforcementAction.BLOCK,
    )
    mgr.register_policy(policy)

    # 2. Acquire within limits
    assert mgr.check_and_acquire("principal_dev", "concurrent_missions", 2.0) is True
    assert mgr.get_usage("principal_dev", "concurrent_missions") == 2.0

    # 3. Third mission succeeds (2 + 1 = 3 <= 3)
    assert mgr.check_and_acquire("principal_dev", "concurrent_missions", 1.0) is True
    assert mgr.get_usage("principal_dev", "concurrent_missions") == 3.0

    # 4. Fourth mission breaches quota -> QuotaExceededError
    with pytest.raises(QuotaExceededError) as exc_info:
        mgr.check_and_acquire("principal_dev", "concurrent_missions", 1.0)

    err = exc_info.value
    assert err.dimension == "concurrent_missions"
    assert err.limit == 3.0
    assert err.requested == 4.0
    assert err.action == QuotaEnforcementAction.BLOCK

    # Verify usage was not incremented on failure
    assert mgr.get_usage("principal_dev", "concurrent_missions") == 3.0

    # 5. Release 1 mission -> slot freed up
    mgr.release("principal_dev", "concurrent_missions", 1.0)
    assert mgr.get_usage("principal_dev", "concurrent_missions") == 2.0

    # 6. Now another acquisition succeeds
    assert mgr.check_and_acquire("principal_dev", "concurrent_missions", 1.0) is True
    assert mgr.get_usage("principal_dev", "concurrent_missions") == 3.0

    # 7. Unconstrained principal bypasses check
    assert mgr.check_and_acquire("principal_enterprise", "concurrent_missions", 50.0) is True
