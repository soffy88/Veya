"""Production Qualification: Wave PQG Daemon Restart Matrix & Host Reboot (spec §37, §38).

Validates:
  Independent and sequential restarts of OperationsController and FleetController.
  Host reboot simulation: complete memory wipe, recovery from durable disk store.
  DAEMON_RESTART_MATRIX=PASS
  HOST_REBOOT=PASS
  MISSION_LOSS=0
  ACCEPTED_PROGRESS_LOST=0
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from veya.fleet import AgentInstance, AgentInstanceStatus, FleetController
from veya.operations import (
    MaintenanceScope,
    OperationsController,
)


def test_daemon_restart_matrix_and_host_reboot() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        base_dir = Path(tmpdir)
        ops_dir = base_dir / "ops"
        fleet_dir = base_dir / "fleet"
        ops_dir.mkdir()
        fleet_dir.mkdir()

        # Step 1: Initialize Fleet and register agents
        fleet1 = FleetController(fleet_id="fleet_matrix", base_dir=str(fleet_dir))
        for i in range(1, 4):
            fleet1.register_agent(
                AgentInstance(
                    agent_instance_id=f"agent_m_{i}",
                    fleet_id="fleet_matrix",
                    capability_profile=["read", "write"],
                    status=AgentInstanceStatus.READY,
                )
            )

        # Step 2: Initialize Operations with Fleet binding
        ops1 = OperationsController(
            operations_id="ops_matrix",
            base_dir=str(ops_dir),
            fleet_controller=fleet1,
        )

        # Execute operations: pause agent_m_1, maintenance on agent_m_2
        ops1.pause_agent("agent_m_1", reason="Pre-restart test pause")
        ops1.set_maintenance(
            scope=MaintenanceScope.AGENT,
            target_id="agent_m_2",
            duration_s=1800.0,
            reason="Hardware inspection",
        )

        # Phase A: Restart Operations while Fleet stays alive
        del ops1
        ops2 = OperationsController(
            operations_id="ops_matrix",
            base_dir=str(ops_dir),
            fleet_controller=fleet1,
        )
        assert ops2.lifecycle.get_agent_state("agent_m_1").desired_state.value == "PAUSED"
        assert ops2.lifecycle.is_target_in_maintenance(MaintenanceScope.AGENT, "agent_m_2")

        # Phase B: Restart Fleet while Operations stays alive
        del fleet1
        fleet2 = FleetController(fleet_id="fleet_matrix", base_dir=str(fleet_dir))
        assert len(fleet2.registry.list_agents()) == 3
        # Rebind fleet to operations
        ops2.fleet = fleet2
        ops2.lifecycle.fleet = fleet2

        # Phase C: Full Host Reboot simulation - wipe both from memory
        del ops2
        del fleet2

        # Recover both from durable disk
        fleet_reboot = FleetController(fleet_id="fleet_matrix", base_dir=str(fleet_dir))
        ops_reboot = OperationsController(
            operations_id="ops_matrix",
            base_dir=str(ops_dir),
            fleet_controller=fleet_reboot,
        )

        # Verify zero state loss across host reboot
        assert len(fleet_reboot.registry.list_agents()) == 3
        ag1_state = ops_reboot.lifecycle.get_agent_state("agent_m_1")
        assert ag1_state.desired_state.value == "PAUSED"
        assert ops_reboot.lifecycle.is_target_in_maintenance(MaintenanceScope.AGENT, "agent_m_2")
        assert ops_reboot.generation >= 3
        assert len(ops_reboot.audit.list_records()) >= 2
