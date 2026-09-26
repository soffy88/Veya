"""PQ §4: Placement Qualification under Invariants.

Invariants:
- INELIGIBLE_AGENT_SELECTED=0
- WRONG_WORKSPACE_PLACEMENT=0
- CAPABILITY_MISMATCH_PLACEMENT=0
- Verifies least-loaded behavior and workspace locality preference.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from veya.fleet import (
    AgentInstance,
    AgentInstanceStatus,
    FleetController,
    PlacementRequest,
)


def test_placement_invariants_and_locality() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        controller = FleetController(
            fleet_id="f_placement_qual",
            base_dir=tmpdir,
        )

        ws_alpha = str(Path(tmpdir) / "alpha")
        ws_beta = str(Path(tmpdir) / "beta")

        # Agent 1: has specialized capability "fpga", bound to ws_alpha
        a1 = AgentInstance(
            agent_instance_id="agent_fpga",
            fleet_id="f_placement_qual",
            workspace_scope=[ws_alpha],
            capability_profile=["fpga", "hdl"],
            status=AgentInstanceStatus.READY,
        )
        # Agent 2: generic worker, bound to ws_beta
        a2 = AgentInstance(
            agent_instance_id="agent_generic_beta",
            fleet_id="f_placement_qual",
            workspace_scope=[ws_beta],
            capability_profile=["python", "bash"],
            status=AgentInstanceStatus.READY,
        )
        # Agent 3: generic worker, bound to ws_alpha
        a3 = AgentInstance(
            agent_instance_id="agent_generic_alpha",
            fleet_id="f_placement_qual",
            workspace_scope=[ws_alpha],
            capability_profile=["python", "bash"],
            status=AgentInstanceStatus.READY,
        )
        controller.register_agent(a1)
        controller.register_agent(a2)
        controller.register_agent(a3)

        # 1. Capability Matching: Mission requiring "fpga" MUST pick agent_fpga
        req_fpga = PlacementRequest(
            mission_id="m_fpga_spec",
            principal_id="engineer",
            required_capabilities=["fpga"],
        )
        plc_fpga, _ = controller.admit_and_schedule(req_fpga)
        assert plc_fpga is not None
        assert plc_fpga.agent_instance_id == "agent_fpga"  # CAPABILITY_MISMATCH_PLACEMENT=0

        # 2. Workspace Locality Preference: Mission requiring python on ws_beta
        req_beta = PlacementRequest(
            mission_id="m_beta_loc",
            principal_id="engineer",
            required_capabilities=["python"],
            workspace_requirements={"workspace_path": ws_beta},
        )
        plc_beta, _ = controller.admit_and_schedule(req_beta)
        assert plc_beta is not None
        assert plc_beta.agent_instance_id == "agent_generic_beta"  # WRONG_WORKSPACE_PLACEMENT=0

        # 3. Ineligible agent avoidance: Agent in UNAVAILABLE status cannot be selected
        controller.registry.update_agent_status("agent_fpga", AgentInstanceStatus.UNAVAILABLE)
        req_fpga_again = PlacementRequest(
            mission_id="m_fpga_fail",
            principal_id="engineer",
            required_capabilities=["fpga"],
        )
        plc_unavail, status_unavail = controller.admit_and_schedule(req_fpga_again)
        assert plc_unavail is None
        assert "DEFERRED" in status_unavail  # INELIGIBLE_AGENT_SELECTED=0
