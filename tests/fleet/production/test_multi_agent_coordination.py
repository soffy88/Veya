"""PQ §13: Multi-Agent Mission Coordination and Reviewer Boundary.

Invariants:
- MULTI_AGENT_MISSION=PASS
- REVIEWER_SEMANTIC_AUTHORITY=0
- MASTER_AGENT_AUTHORITY_DRIFT=0
"""

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


def test_multi_agent_coordination_and_reviewer_authority_boundaries() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        controller = FleetController(fleet_id="f_collab_pqc", base_dir=tmpdir)

        # Register PRIMARY, WORKER, and REVIEWER instances
        for name, role_cap in [
            ("agent_lead", "planning"),
            ("agent_executor", "codegen"),
            ("agent_evaluator", "review"),
        ]:
            agent = AgentInstance(
                agent_instance_id=name,
                fleet_id="f_collab_pqc",
                capability_profile=[role_cap],
                status=AgentInstanceStatus.READY,
            )
            controller.register_agent(agent)

        mission_id = "m_prod_collab_arch"

        # 1. Primary MasterAgent initiates child task for Worker
        worker_assignment = controller.collaborations.assign_child_task(
            mission_id=mission_id,
            goal_task_id="task_implement_service",
            agent_instance_id="agent_executor",
            role=AgentRole.WORKER,
            expected_output="Service source code",
        )
        assert worker_assignment.role == AgentRole.WORKER

        # Worker completes execution and provides evidence
        controller.collaborations.submit_worker_output(
            collaboration_id=worker_assignment.collaboration_id,
            evidence_refs=["ev_service_src", "ev_unit_test"],
            execution_report="Implemented service handler",
        )

        # Worker cannot finalize or seal mission
        with pytest.raises(CollaborationAuthorityError):
            controller.collaborations.submit_worker_output(
                collaboration_id=worker_assignment.collaboration_id,
                evidence_refs=["ev_service_src"],
                attempted_mission_completion=True,
            )

        # 2. MasterAgent initiates child task for Reviewer
        reviewer_assignment = controller.collaborations.assign_child_task(
            mission_id=mission_id,
            goal_task_id="task_audit_service",
            agent_instance_id="agent_evaluator",
            role=AgentRole.REVIEWER,
            input_refs=["ev_service_src"],
            expected_output="Security and defect report",
        )
        assert reviewer_assignment.role == AgentRole.REVIEWER

        # Reviewer submits critique
        report = controller.collaborations.submit_reviewer_recommendation(
            collaboration_id=reviewer_assignment.collaboration_id,
            recommendation="ACCEPT",
            critique="Implementation meets security requirements",
            defects=[],
        )
        assert report["recommendation"] == "ACCEPT"
        assert report["semantic_authority"] == "MasterAgent"

        # Reviewer cannot declare completion or finalize mission (REVIEWER_SEMANTIC_AUTHORITY=0)
        with pytest.raises(CollaborationAuthorityError):
            controller.collaborations.submit_reviewer_recommendation(
                collaboration_id=reviewer_assignment.collaboration_id,
                recommendation="ACCEPT",
                attempted_direct_completion=True,
            )
