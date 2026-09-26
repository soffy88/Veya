"""Production Qualification: Desired vs Observed State Convergence (spec §10).

Validates:
  desired_state != observed_state
  Controller converges observed state through canonical Fleet/AgentRuntime contracts.
  DIRECT_STATE_FORGERY=0.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from veya.fleet import AgentInstance, AgentInstanceStatus, FleetController
from veya.operations import AgentOperationalStatus, OperationsController


def test_desired_observed_state_convergence() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        base_dir = Path(tmpdir)
        fleet = FleetController(fleet_id="f_conv", base_dir=str(base_dir / "fleet"))
        fleet.register_agent(
            AgentInstance(
                agent_instance_id="agent_conv_1",
                fleet_id="f_conv",
                status=AgentInstanceStatus.READY,
            )
        )

        ops = OperationsController(base_dir=str(base_dir / "ops"), fleet_controller=fleet)

        # 1. Manually simulate state discrepancy
        state = ops.lifecycle.get_agent_state("agent_conv_1")
        state.desired_state = AgentOperationalStatus.PAUSED
        state.observed_state = AgentOperationalStatus.ACTIVE

        # Discrepancy present
        assert state.desired_state != state.observed_state

        # 2. Invoke convergence reconciliation through pause_agent
        ops.pause_agent("agent_conv_1", reason="Reconciling desired state")

        # 3. Verified converged
        converged_state = ops.lifecycle.get_agent_state("agent_conv_1")
        assert converged_state.desired_state == AgentOperationalStatus.PAUSED
        assert converged_state.observed_state == AgentOperationalStatus.PAUSED
        assert fleet.registry.get_agent("agent_conv_1").status == AgentInstanceStatus.WAITING

        # 4. Now desired = ACTIVE, observed = PAUSED
        ops.resume_agent("agent_conv_1", reason="Reconciling resume")
        resumed_state = ops.lifecycle.get_agent_state("agent_conv_1")
        assert resumed_state.desired_state == AgentOperationalStatus.ACTIVE
        assert resumed_state.observed_state == AgentOperationalStatus.ACTIVE
        assert fleet.registry.get_agent("agent_conv_1").status == AgentInstanceStatus.READY
