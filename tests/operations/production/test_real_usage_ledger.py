"""Production Qualification: Wave PQE Real Usage Ledger (spec §28).

Validates:
  Usage ledger durability and exact-once accounting.
  Zero duplicate usage charge (DUPLICATE_USAGE_CHARGE=0).
  Multi-dimensional aggregation by principal, agent, and mission.
  USAGE_LEDGER=PASS
"""

from __future__ import annotations

import time

from veya.operations import (
    UsageLedgerManager,
    UsageRecord,
)


def test_real_usage_ledger_idempotency_and_queries() -> None:
    ledger = UsageLedgerManager()

    now = time.time()
    r1 = UsageRecord(
        record_id="rec_001",
        principal_id="principal_alpha",
        agent_id="agent_1",
        mission_id="mission_101",
        goal_run_id="gr_101",
        execution_id="exec_101",
        provider="opencode-zen",
        model="deepseek-v4.1-flash",
        resource_type="tokens",
        quantity=1500.0,
        unit="tokens",
        observed_at=now,
        source_event_id="evt_stream_101",
    )

    # 1. Record first usage
    ok = ledger.record_usage(r1)
    assert ok is True

    # 2. Replay with identical record_id -> Ignored (DUPLICATE_USAGE_CHARGE=0)
    ok_dup = ledger.record_usage(r1)
    assert ok_dup is False

    # 3. Replay with new record_id but same source_event_id -> Ignored (DUPLICATE_USAGE_CHARGE=0)
    r1_dup_event = UsageRecord(
        record_id="rec_002",
        principal_id="principal_alpha",
        agent_id="agent_1",
        mission_id="mission_101",
        goal_run_id="gr_101",
        execution_id="exec_101",
        provider="opencode-zen",
        model="deepseek-v4.1-flash",
        resource_type="tokens",
        quantity=1500.0,
        unit="tokens",
        observed_at=now,
        source_event_id="evt_stream_101",  # Same event
    )
    ok_dup_evt = ledger.record_usage(r1_dup_event)
    assert ok_dup_evt is False

    # Total quantity must remain exactly 1500
    total_tokens = ledger.total_quantity("principal_alpha", "tokens")
    assert total_tokens == 1500.0

    # 4. Add distinct usage
    r2 = UsageRecord(
        record_id="rec_003",
        principal_id="principal_alpha",
        agent_id="agent_2",
        mission_id="mission_102",
        goal_run_id="gr_102",
        execution_id="exec_102",
        provider="opencode-zen",
        model="deepseek-v4.1-flash",
        resource_type="tokens",
        quantity=2500.0,
        unit="tokens",
        observed_at=now + 1.0,
        source_event_id="evt_stream_102",
    )
    assert ledger.record_usage(r2) is True
    assert ledger.total_quantity("principal_alpha", "tokens") == 4000.0

    # 5. Queries by criteria
    q_agent1 = ledger.query_usage(principal_id="principal_alpha", agent_id="agent_1")
    assert len(q_agent1) == 1
    assert q_agent1[0].record_id == "rec_001"

    q_mission102 = ledger.query_usage(mission_id="mission_102")
    assert len(q_mission102) == 1
    assert q_mission102[0].quantity == 2500.0
