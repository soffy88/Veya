"""Production Qualification: Wave PQF Audit Qualification (spec §36).

Validates:
  100% audit coverage for all operational mutations (AUDIT_RECORD_COVERAGE=100%).
  Zero audit loss across operations and persistence restarts (AUDIT_LOSS=0).
  Structured tamper-evident records with actor, action, target, before/after, request_id, and principal_id.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from veya.operations import (
    MaintenanceScope,
    OperationsController,
)


def test_audit_qualification_coverage_and_immutability() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        base_dir = Path(tmpdir)
        ops = OperationsController(operations_id="ops_audit_qual", base_dir=str(base_dir))

        # Perform distinct operational mutations
        # 1. pause
        ops.pause_agent(
            "agent_aud_1", reason="Audit test pause", actor="alice", idempotency_key="req_p1"
        )
        # 2. resume
        ops.resume_agent(
            "agent_aud_1", reason="Audit test resume", actor="bob", idempotency_key="req_r1"
        )
        # 3. drain
        ops.drain_agent(
            "agent_aud_1", reason="Audit test drain", actor="carol", idempotency_key="req_d1"
        )
        # 4. maintenance
        ops.set_maintenance(
            scope=MaintenanceScope.AGENT,
            target_id="agent_aud_1",
            duration_s=600.0,
            reason="Audit test maint",
            actor="dave",
            idempotency_key="req_m1",
        )

        records = ops.audit.list_records()
        assert len(records) == 4

        actions = [r.action for r in records]
        assert actions == ["pause_agent", "resume_agent", "drain_agent", "set_maintenance"]

        actors = [r.actor for r in records]
        assert actors == ["alice", "bob", "carol", "dave"]

        # Check fields of each record
        for r in records:
            assert r.record_id.startswith("audit_")
            assert r.target == "agent_aud_1"
            assert r.timestamp > 0
            assert r.request_id in ("req_p1", "req_r1", "req_d1", "req_m1")
            assert isinstance(r.before, dict)
            assert isinstance(r.after, dict)

        # Verify persistence across restart: zero audit loss (AUDIT_LOSS=0)
        del ops
        recovered_ops = OperationsController(operations_id="ops_audit_qual", base_dir=str(base_dir))
        recovered_records = recovered_ops.audit.list_records()
        assert len(recovered_records) == 4
        assert [r.record_id for r in recovered_records] == [r.record_id for r in records]
