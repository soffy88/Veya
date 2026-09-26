"""Tests for Multi-Agent Collaboration, Reviewer Authority Boundary, and Handoff (AF-P5, E2E-I, E2E-J)."""

from __future__ import annotations

import tempfile

import pytest

from veya.fleet import (
    AgentInstance,
    AgentInstanceStatus,
    AgentRole,
    CollaborationAuthorityError,
    FleetController,
)


def test_multi_agent_mission_and_reviewer_authority_e2e_i_e2e_j() -> None:
    """E2E-I & E2E-J: MasterAgent assigns child tasks to worker and reviewer. Worker/reviewer cannot finalize mission."""
    with tempfile.TemporaryDirectory() as tmpdir:
        controller = FleetController(fleet_id="f_collab", base_dir=tmpdir)

        # Register primary, worker, and reviewer instances
        for name, role in [
            ("agent_primary", "leader"),
            ("agent_worker", "builder"),
            ("agent_reviewer", "auditor"),
        ]:
            agent = AgentInstance(
                agent_instance_id=name,
                fleet_id="f_collab",
                capability_profile=["coding", role],
                status=AgentInstanceStatus.READY,
            )
            controller.register_agent(agent)

        # MasterAgent coordinates child tasks
        # 1. Assign worker task
        worker_task = controller.collaborations.assign_child_task(
            mission_id="m_collab_1",
            goal_task_id="task_build_api",
            agent_instance_id="agent_worker",
            role=AgentRole.WORKER,
            expected_output="API module code and test results",
        )
        assert worker_task.role == AgentRole.WORKER

        # Worker completes task and returns evidence
        controller.collaborations.submit_worker_output(
            collaboration_id=worker_task.collaboration_id,
            evidence_refs=["ev_api_ok", "ev_unit_test_green"],
            execution_report="API endpoint successfully created",
        )

        # Worker attempting to finalize mission must be denied (WORKER_MISSION_FINALIZE=0)
        with pytest.raises(CollaborationAuthorityError):
            controller.collaborations.submit_worker_output(
                collaboration_id=worker_task.collaboration_id,
                evidence_refs=["ev_api_ok"],
                attempted_mission_completion=True,
            )

        # 2. Assign reviewer task
        reviewer_task = controller.collaborations.assign_child_task(
            mission_id="m_collab_1",
            goal_task_id="task_audit_api",
            agent_instance_id="agent_reviewer",
            role=AgentRole.REVIEWER,
            input_refs=["ev_api_ok"],
        )

        # Reviewer submits evaluation with defects
        review_result = controller.collaborations.submit_reviewer_recommendation(
            collaboration_id=reviewer_task.collaboration_id,
            recommendation="REJECT",
            critique="Missing rate limit guard",
            defects=["NO_RATE_LIMITER"],
        )
        assert review_result["recommendation"] == "REJECT"
        assert review_result["semantic_authority"] == "MasterAgent"

        # Reviewer attempting direct completion must be denied (REVIEWER_COMPLETION_AUTHORITY=0)
        with pytest.raises(CollaborationAuthorityError):
            controller.collaborations.submit_reviewer_recommendation(
                collaboration_id=reviewer_task.collaboration_id,
                recommendation="ACCEPT",
                attempted_direct_completion=True,
            )


def test_cross_agent_handoff_preconditions() -> None:
    """Cross-agent handoff validates target capabilities and fails closed on deficit."""
    with tempfile.TemporaryDirectory() as tmpdir:
        controller = FleetController(fleet_id="f_handoff", base_dir=tmpdir)
        agent_target = AgentInstance(
            agent_instance_id="agent_target",
            fleet_id="f_handoff",
            capability_profile=["read_only"],
            status=AgentInstanceStatus.READY,
        )
        controller.register_agent(agent_target)

        # Attempt handoff requiring 'write_access' -> must fail closed
        with pytest.raises(CollaborationAuthorityError):
            controller.collaborations.handoff_mission(
                mission_id="m_hand",
                source_agent="agent_source",
                target_agent="agent_target",
                reason="Load rebalancing",
                session_envelope_ref="env_123",
                accepted_progress_ref="prog_123",
                workspace_ref="ws_123",
                target_capabilities=["write_access"],
            )

        # Valid handoff with available capability succeeds
        handoff = controller.collaborations.handoff_mission(
            mission_id="m_hand",
            source_agent="agent_source",
            target_agent="agent_target",
            reason="Load rebalancing",
            session_envelope_ref="env_123",
            accepted_progress_ref="prog_123",
            workspace_ref="ws_123",
            target_capabilities=["read_only"],
        )
        assert handoff.handoff_id.startswith("handoff_")
