"""Production Qualification: Wave PQE Approval Boundary (spec §31).

Validates:
  High-impact operational mutations require explicit verified approvals.
  Zero approval bypass (OPERATIONAL_APPROVAL_BYPASS=0).
  APPROVAL_BYPASS=0
"""

from __future__ import annotations

import tempfile

import pytest

from veya.operations import (
    MaintenanceScope,
    OperationalApprovalRequiredError,
    OperationsController,
)


def test_high_impact_operational_approval_boundary() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        ops = OperationsController(operations_id="ops_approval_test", base_dir=tmpdir)

        # 1. fleet_maintenance without approval -> OperationalApprovalRequiredError
        with pytest.raises(OperationalApprovalRequiredError) as exc_info:
            ops.set_maintenance(
                scope=MaintenanceScope.FLEET,
                target_id="fleet_primary",
                duration_s=1800.0,
                reason="Fleet-wide OS upgrade",
                approval_id=None,
            )
        assert "OPERATIONAL_APPROVAL_REQUIRED" in str(exc_info.value)

        # 2. fleet_maintenance with unregistered bogus approval -> Denied
        with pytest.raises(OperationalApprovalRequiredError):
            ops.set_maintenance(
                scope=MaintenanceScope.FLEET,
                target_id="fleet_primary",
                duration_s=1800.0,
                reason="Fleet-wide OS upgrade",
                approval_id="unregistered_fake_approval",
            )

        # 3. Register valid approval ID in audit governance
        valid_approval_id = "appr_change_request_4821"
        ops.audit.register_approval(valid_approval_id)

        # 4. Now fleet_maintenance succeeds
        mw = ops.set_maintenance(
            scope=MaintenanceScope.FLEET,
            target_id="fleet_primary",
            duration_s=1800.0,
            reason="Fleet-wide OS upgrade",
            approval_id=valid_approval_id,
        )
        assert mw.id in ops.lifecycle._maintenance_windows
        assert ops.lifecycle.is_target_in_maintenance(MaintenanceScope.FLEET, "fleet_primary")

        # Verify audit record notes the approval ID
        records = ops.audit.list_records()
        fleet_maint_recs = [r for r in records if r.action == "set_maintenance"]
        assert len(fleet_maint_recs) == 1
        assert fleet_maint_recs[0].approval_id == valid_approval_id
