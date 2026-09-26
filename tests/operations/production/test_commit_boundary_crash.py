"""Production Qualification: Wave PQC Commit Boundary Crash & Idempotency (spec §18).

Validates:
  Atomic persistence boundaries prevent half-written/corrupt state files.
  Idempotency enforcement across replayed commands.
  DUPLICATE_OPERATIONAL_SIDE_EFFECTS=0
  OPERATIONS_CONTROLLER_RECOVERY=PASS
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from veya.operations import (
    DuplicateOperationalRequestError,
    MaintenanceScope,
    OperationsController,
)


def test_commit_boundary_atomic_write_and_idempotency_protection() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        base_dir = Path(tmpdir)
        ops = OperationsController(operations_id="ops_commit_boundary", base_dir=str(base_dir))

        # 1. Execute an operation with unique idempotency key
        key = "idemp_boundary_key_101"
        ops.set_maintenance(
            scope=MaintenanceScope.AGENT,
            target_id="agent_commit_test",
            duration_s=600.0,
            reason="Drain for inspection",
            idempotency_key=key,
        )

        # 2. Duplicate operation with same key must be rejected
        with pytest.raises(DuplicateOperationalRequestError):
            ops.set_maintenance(
                scope=MaintenanceScope.AGENT,
                target_id="agent_commit_test",
                duration_s=600.0,
                reason="Drain for inspection (retry)",
                idempotency_key=key,
            )

        # Verify side effects occurred exactly once
        assert ops.lifecycle.is_target_in_maintenance(MaintenanceScope.AGENT, "agent_commit_test")
        windows = [
            w
            for w in ops.lifecycle._maintenance_windows.values()
            if w.target_id == "agent_commit_test"
        ]
        assert len(windows) == 1

        # 3. Simulate failure right around temp file write
        state_file = base_dir / "operations_state.json"
        assert state_file.exists()
        original_content = state_file.read_text()
        assert len(original_content) > 0

        # Place a partial junk file like operations_state.tmp.12345
        junk_tmp = base_dir / "operations_state.tmp.99999"
        junk_tmp.write_text("{corrupt partial json")

        # Restart controller: must read intact operations_state.json, ignore stale temp files
        del ops
        recovered_ops = OperationsController(
            operations_id="ops_commit_boundary", base_dir=str(base_dir)
        )
        assert recovered_ops.lifecycle.is_target_in_maintenance(
            MaintenanceScope.AGENT, "agent_commit_test"
        )

        # Re-verify duplicate key rejection persists across restarts
        with pytest.raises(DuplicateOperationalRequestError):
            recovered_ops.pause_agent(
                agent_id="agent_commit_test",
                idempotency_key=key,
            )
