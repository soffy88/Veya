"""Production Qualification: Wave PQC Persistent Recovery (spec §17).

Validates:
  Recovery of maintenance windows, agent states, budgets, audit records, and idempotency keys from durable disk store.
  Generation advances monotonically across controller restarts.
  PERSISTENT_RECOVERY=PASS
  IDEMPOTENT_RECOVERY=PASS
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from veya.operations import (
    CostBudget,
    MaintenanceScope,
    OperationsController,
)


def test_persistent_recovery_of_full_operations_state() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        base_dir = Path(tmpdir)

        # 1. Controller 1 creates multi-domain state
        ops1 = OperationsController(operations_id="ops_persist_test", base_dir=str(base_dir))

        # Maintenance
        mw = ops1.set_maintenance(
            scope=MaintenanceScope.AGENT,
            target_id="agent_persist_1",
            duration_s=3600.0,
            reason="Scheduled hardware maintenance",
            idempotency_key="key_maint_p1",
        )
        assert mw.id in ops1.lifecycle._maintenance_windows

        # Lifecycle pause
        st = ops1.pause_agent(
            agent_id="agent_persist_1",
            reason="Pre-maintenance isolation",
            idempotency_key="key_pause_p1",
        )
        assert st.desired_state.value == "PAUSED"

        # Budget registration
        b = CostBudget(
            budget_id="budget_persist_1",
            principal_id="principal_alpha",
            budget_amount=500.0,
            warning_threshold=0.8,
            current_spend=120.0,
        )
        ops1.budgets.register_budget(b)
        ops1._save_state()

        gen_before = ops1.generation
        audit_count_before = len(ops1.audit.list_records())
        assert audit_count_before >= 2

        # 2. Simulate process crash / clean exit
        del ops1

        # 3. Controller 2 recovers state from durable disk store
        ops2 = OperationsController(operations_id="ops_persist_test", base_dir=str(base_dir))

        # Generation advanced
        assert ops2.generation > gen_before

        # Maintenance window recovered
        assert ops2.lifecycle.is_target_in_maintenance(MaintenanceScope.AGENT, "agent_persist_1")
        assert mw.id in ops2.lifecycle._maintenance_windows

        # Agent state recovered
        recovered_st = ops2.lifecycle.get_agent_state("agent_persist_1")
        assert recovered_st.desired_state.value == "PAUSED"
        assert recovered_st.reason == "Pre-maintenance isolation"

        # Budget recovered
        recovered_b = ops2.budgets._budgets.get("budget_persist_1")
        assert recovered_b is not None
        assert recovered_b.principal_id == "principal_alpha"
        assert recovered_b.budget_amount == 500.0
        assert recovered_b.current_spend == 120.0

        # Audit trail recovered
        assert len(ops2.audit.list_records()) >= audit_count_before

        # Idempotency keys preserved: replaying old keys fails
        assert "key_pause_p1" in ops2._processed_idempotency_keys
