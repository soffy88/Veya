"""PQ §14: Agent Handoff, Session Lineage, and Context Continuity.

Invariants:
- HANDOFF=PASS
- SESSION_LINEAGE_PRESERVED=YES
"""

from __future__ import annotations

import tempfile

import pytest

from veya.fleet import (
    AgentInstance,
    AgentInstanceStatus,
    CollaborationAuthorityError,
    FleetController,
)


def test_agent_handoff_and_session_lineage_qualification() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        controller = FleetController(fleet_id="f_handoff_pqc", base_dir=tmpdir)

        # Source agent
        agent_src = AgentInstance(
            agent_instance_id="agent_specialist_a",
            fleet_id="f_handoff_pqc",
            capability_profile=["research"],
            status=AgentInstanceStatus.READY,
        )
        # Target agent with coding capability
        agent_dst = AgentInstance(
            agent_instance_id="agent_specialist_b",
            fleet_id="f_handoff_pqc",
            capability_profile=["coding", "git"],
            status=AgentInstanceStatus.READY,
        )
        controller.register_agent(agent_src)
        controller.register_agent(agent_dst)

        mission_id = "m_handoff_pipeline"
        envelope_ref = "session_envelope_m_handoff_pipeline_001"
        progress_ref = "prog_accepted_m_handoff_pipeline_v1"
        workspace_ref = "/workspaces/pipeline_repo"

        # 1. Successful handoff when target satisfies required capabilities
        handoff = controller.collaborations.handoff_mission(
            mission_id=mission_id,
            source_agent="agent_specialist_a",
            target_agent="agent_specialist_b",
            reason="Research phase completed, handoff to coding specialist",
            session_envelope_ref=envelope_ref,
            accepted_progress_ref=progress_ref,
            workspace_ref=workspace_ref,
            target_capabilities=["coding"],
        )

        assert handoff is not None
        assert handoff.mission_id == mission_id
        assert handoff.session_envelope_ref == envelope_ref  # SESSION_LINEAGE_PRESERVED=YES
        assert handoff.accepted_progress_ref == progress_ref
        assert handoff.source_agent == "agent_specialist_a"
        assert handoff.target_agent == "agent_specialist_b"

        # 2. Handoff fails closed if target agent lacks capability
        with pytest.raises(CollaborationAuthorityError):
            controller.collaborations.handoff_mission(
                mission_id=mission_id,
                source_agent="agent_specialist_a",
                target_agent="agent_specialist_b",
                reason="Attempt invalid handoff",
                session_envelope_ref=envelope_ref,
                accepted_progress_ref=progress_ref,
                workspace_ref=workspace_ref,
                target_capabilities=["unsupported_hardware_access"],
            )
