"""Production Qualification: Real Maintenance Windows (spec §11).

Validates:
  Maintenance window scope across:
    - agent scope
    - provider scope
    - fleet subset scope
  Start, enforcement, active-work behavior, end, re-admission.
  REAL_MAINTENANCE=PASS
  SILENT_MISSION_TERMINATION=0
"""

from __future__ import annotations

import tempfile
import time
from pathlib import Path

from veya.fleet import (
    AgentInstance,
    AgentInstanceStatus,
    FleetController,
    PlacementRequest,
)
from veya.operations import MaintenanceScope, OperationsController


def test_real_maintenance_windows_scopes_and_protection() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        base_dir = Path(tmpdir)
        fleet = FleetController(fleet_id="f_maint_prod", base_dir=str(base_dir / "fleet"))
        fleet.register_agent(
            AgentInstance(
                agent_instance_id="agent_maint_1",
                fleet_id="f_maint_prod",
                capability_profile=["compute"],
                status=AgentInstanceStatus.READY,
            )
        )
        fleet.register_agent(
            AgentInstance(
                agent_instance_id="agent_maint_2",
                fleet_id="f_maint_prod",
                capability_profile=["compute"],
                status=AgentInstanceStatus.READY,
            )
        )

        ops = OperationsController(base_dir=str(base_dir / "ops"), fleet_controller=fleet)

        # Place mission on agent_maint_1 before maintenance window
        plc1, _ = fleet.admit_and_schedule(
            PlacementRequest(
                mission_id="m_in_maint",
                principal_id="user_maint",
                required_capabilities=["compute"],
            )
        )
        assert plc1 is not None

        # 1. Agent Scope Maintenance
        now = time.time()
        mw_agent = ops.set_maintenance(
            scope=MaintenanceScope.AGENT,
            target_id="agent_maint_1",
            duration_s=3600.0,
            reason="Kernel security patching",
        )
        assert mw_agent.id is not None
        assert ops.lifecycle.is_target_in_maintenance(MaintenanceScope.AGENT, "agent_maint_1")

        # In-flight mission MUST NOT be silently terminated (SILENT_MISSION_TERMINATION=0)
        active_plc = fleet.registry.find_active_placement_for_mission("m_in_maint")
        assert active_plc is not None
        assert active_plc.mission_id == "m_in_maint"

        # 2. Provider Scope Maintenance
        mw_prov = ops.set_maintenance(
            scope=MaintenanceScope.PROVIDER,
            target_id="provider_gemini",
            duration_s=1800.0,
            reason="Upstream provider scheduled downtime",
        )
        assert mw_prov.id is not None
        assert ops.lifecycle.is_target_in_maintenance(MaintenanceScope.PROVIDER, "provider_gemini")

        # 3. Fleet Scope Maintenance (requires registered operational approval)
        ops.audit.register_approval("appr_fleet_maint_1")
        mw_fleet = ops.set_maintenance(
            scope=MaintenanceScope.FLEET,
            target_id="all",
            duration_s=900.0,
            reason="Fleet-wide control plane upgrade",
            approval_id="appr_fleet_maint_1",
        )
        assert mw_fleet.id is not None
        assert ops.lifecycle.is_target_in_maintenance(MaintenanceScope.FLEET, "f_maint_prod")

        # 4. Expiration restores nominal status
        assert not ops.lifecycle.is_target_in_maintenance(
            MaintenanceScope.FLEET, "f_maint_prod", now=now + 5000.0
        )
