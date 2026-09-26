"""PQ §17: Security Isolation and Multi-Tenant Boundaries.

Invariants:
- SECURITY=PASS
- CROSS_PRINCIPAL_LEAK=0
- CROSS_MISSION_MUTATION=0
- CREDENTIAL_LEAK=0
"""

from __future__ import annotations

import tempfile

import pytest

from veya.fleet import (
    AgentInstance,
    AgentInstanceStatus,
    FleetController,
    LeaseConflictError,
    PlacementRequest,
)


def test_security_isolation_and_cross_principal_boundaries() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        controller = FleetController(fleet_id="f_sec_prod", base_dir=tmpdir)

        a1 = AgentInstance(
            agent_instance_id="agent_tenant_a",
            fleet_id="f_sec_prod",
            principal_id="tenant_a",
            workspace_scope=["/secure/tenant_a/workspace"],
            status=AgentInstanceStatus.READY,
        )
        a2 = AgentInstance(
            agent_instance_id="agent_tenant_b",
            fleet_id="f_sec_prod",
            principal_id="tenant_b",
            workspace_scope=["/secure/tenant_b/workspace"],
            status=AgentInstanceStatus.READY,
        )
        controller.register_agent(a1)
        controller.register_agent(a2)

        # 1. Tenant A placement
        req_a = PlacementRequest(
            mission_id="m_sec_a",
            principal_id="tenant_a",
            workspace_requirements={"workspace_path": "/secure/tenant_a/workspace"},
        )
        plc_a, _ = controller.admit_and_schedule(req_a)
        assert plc_a is not None
        assert plc_a.agent_instance_id == "agent_tenant_a"

        # 2. Cross-agent lease ownership: Agent B cannot acquire/usurp lease for Tenant A's active mission
        with pytest.raises(LeaseConflictError):
            controller.leases.acquire_lease(
                agent_instance_id="agent_tenant_b",
                mission_id="m_sec_a",
                allow_parallel=False,
            )

        # 3. Secret leak check in fleet state (spec §51: SECRET_IN_FLEET_STATE=0)
        # Fleet manifests only store capability and requirement refs, never secrets
        plc_record = controller.registry.get_placement(plc_a.placement_id)
        assert plc_record is not None
        manifest_text = str(plc_record.manifest_snapshot)
        assert "password" not in manifest_text.lower()
        assert "secret_key" not in manifest_text.lower()
        assert "private_key" not in manifest_text.lower()
