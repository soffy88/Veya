"""Tests for Cross-Principal Security and Workspace Mutation Locks (spec §14, §15, §50, E2E-K)."""

from __future__ import annotations

import tempfile

from veya.fleet import (
    AgentInstance,
    AgentInstanceStatus,
    FleetController,
    PlacementRequest,
)


def test_cross_principal_isolation_and_workspace_locks_e2e_k() -> None:
    """E2E-K: Workspace mutation lock prevents concurrent writes on same workspace; principal limits respected."""
    with tempfile.TemporaryDirectory() as tmpdir:
        controller = FleetController(
            fleet_id="f_sec",
            base_dir=tmpdir,
            per_principal_max_missions=2,
        )

        for i in range(1, 3):
            agent = AgentInstance(
                agent_instance_id=f"agent_iso_{i}",
                fleet_id="f_sec",
                capability_profile=["coding"],
                status=AgentInstanceStatus.READY,
            )
            controller.register_agent(agent)

        # Principal Alice requests mutating mission on /workspaces/repo_alpha
        req_alice = PlacementRequest(
            mission_id="m_alice_1",
            principal_id="alice",
            required_capabilities=["coding"],
            workspace_requirements={
                "workspace_path": "/workspaces/repo_alpha",
                "requires_mutation": True,
            },
        )
        plc_a, _ = controller.admit_and_schedule(req_alice)
        assert plc_a is not None

        # Principal Bob requests concurrent mutating mission on same /workspaces/repo_alpha
        req_bob = PlacementRequest(
            mission_id="m_bob_1",
            principal_id="bob",
            required_capabilities=["coding"],
            workspace_requirements={
                "workspace_path": "/workspaces/repo_alpha",
                "requires_mutation": True,
            },
        )
        plc_b, status_b = controller.admit_and_schedule(req_bob)
        # Must be DEFERRED due to workspace mutation exclusion (spec §15)
        assert plc_b is None
        assert "WORKSPACE_MUTATION_LOCK" in status_b

        # Principal Bob requests read-only mission on /workspaces/repo_alpha
        req_bob_ro = PlacementRequest(
            mission_id="m_bob_ro",
            principal_id="bob",
            required_capabilities=["coding"],
            workspace_requirements={
                "workspace_path": "/workspaces/repo_alpha",
                "requires_mutation": False,
            },
        )
        plc_b_ro, status_b_ro = controller.admit_and_schedule(req_bob_ro)
        assert plc_b_ro is not None
        assert status_b_ro == "PLACED"
