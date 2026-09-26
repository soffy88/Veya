"""PQ §15: Provider Failure Domain and Zero Silent Substitution.

Invariants:
- SILENT_PROVIDER_FALLBACK=0
- SILENT_MODEL_SUBSTITUTION=0
- PROVIDER_FAILURE_RECOVERY=PASS
- MISSION_STATE=BLOCKED when no legal alternative exists.
"""

from __future__ import annotations

import tempfile

from veya.fleet import (
    AgentInstance,
    AgentInstanceStatus,
    FleetController,
    PlacementRequest,
)


def test_provider_failure_domain_and_zero_silent_substitution() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        controller = FleetController(fleet_id="f_provider_fail", base_dir=tmpdir)

        # Agent 1 and Agent 2 share provider failure domain "provider_cluster_east"
        a1 = AgentInstance(
            agent_instance_id="agent_east_1",
            fleet_id="f_provider_fail",
            provider="opencode_east",
            failure_domain="provider_cluster_east",
            capability_profile=["deepseek_v4"],
            status=AgentInstanceStatus.READY,
        )
        # Agent 3 belongs to independent failure domain "provider_cluster_west" with different capability
        a3 = AgentInstance(
            agent_instance_id="agent_west_1",
            fleet_id="f_provider_fail",
            provider="opencode_west",
            failure_domain="provider_cluster_west",
            capability_profile=["gpt5_luna"],
            status=AgentInstanceStatus.READY,
        )
        controller.register_agent(a1)
        controller.register_agent(a3)

        # 1. Mission placed on east agent requesting deepseek_v4
        req = PlacementRequest(
            mission_id="m_strict_model",
            principal_id="enterprise",
            required_capabilities=["deepseek_v4"],
        )
        plc, _ = controller.admit_and_schedule(req)
        assert plc is not None
        assert plc.agent_instance_id == "agent_east_1"

        # 2. East provider experiences outage -> agent_east_1 becomes UNAVAILABLE
        controller.registry.update_agent_status("agent_east_1", AgentInstanceStatus.UNAVAILABLE)

        # 3. Requesting replacement for deepseek_v4 when only gpt5_luna exists:
        # Fleet MUST NOT silently substitute gpt5_luna (SILENT_MODEL_SUBSTITUTION=0)
        candidates = controller.scheduler.find_candidates(req)
        assert len(candidates) == 0, "No agent satisfies deepseek_v4, must not substitute"

        # Attempting recovery placement yields failure/blocked
        req_blocked = PlacementRequest(
            mission_id="m_strict_model_retry",
            principal_id="enterprise",
            required_capabilities=["deepseek_v4"],
        )
        plc_retry, status_retry = controller.admit_and_schedule(req_blocked)
        assert plc_retry is None
        assert "DEFERRED" in status_retry or "REJECTED" in status_retry
