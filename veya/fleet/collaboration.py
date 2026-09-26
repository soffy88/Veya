"""Multi-Agent Collaboration, Handoff, and Reviewer Authority Boundary (spec §30-36, §61).

Invariants:
- SECOND_MASTER_PATH=0: Only one MasterAgent semantic authority per Mission.
- WORKER_MISSION_FINALIZE=0: Workers return execution results/evidence only, never finalize mission.
- REVIEWER_COMPLETION_AUTHORITY=0: Reviewers recommend accept/reject only; MasterAgent decides completion.
- MULTI_MASTER_SEMANTIC_FORK=0: Child goals execute via fleet placement without semantic forks.
- Cross-agent handoffs validate preconditions and fail closed.
"""

from __future__ import annotations

import threading
import time
import uuid
from typing import Any

from .models import (
    AgentHandoff,
    AgentRole,
    CollaborationAssignment,
)
from .registry import FleetRegistry


class CollaborationAuthorityError(RuntimeError):
    """Raised when an agent role attempts an action beyond its authority."""


class CollaborationManager:
    """Coordinates multi-agent assignments, handoffs, and enforces authority boundaries."""

    def __init__(self, registry: FleetRegistry):
        self.registry = registry
        self._lock = threading.RLock()

    def assign_child_task(
        self,
        mission_id: str,
        goal_task_id: str,
        agent_instance_id: str,
        role: AgentRole = AgentRole.WORKER,
        scope: str = "",
        input_refs: list[str] | None = None,
        expected_output: str = "",
    ) -> CollaborationAssignment:
        """Assign a child task context to an agent instance."""
        with self._lock:
            # Check agent existence
            agent = self.registry.get_agent(agent_instance_id)
            if not agent:
                raise KeyError(f"Agent instance '{agent_instance_id}' not found")

            collab_id = f"collab_{uuid.uuid4().hex[:12]}"
            assignment = CollaborationAssignment(
                collaboration_id=collab_id,
                mission_id=mission_id,
                goal_task_id=goal_task_id,
                agent_instance_id=agent_instance_id,
                role=role,
                scope=scope,
                input_refs=input_refs or [],
                expected_output=expected_output,
                status="ASSIGNED",
                result_evidence=[],
            )
            self.registry.save_collaboration(assignment)
            return assignment

    def submit_worker_output(
        self,
        collaboration_id: str,
        evidence_refs: list[str],
        execution_report: str = "",
        attempted_mission_completion: bool = False,
    ) -> CollaborationAssignment:
        """Record worker task output. Strictly forbids worker mission finalization (WORKER_MISSION_FINALIZE=0)."""
        with self._lock:
            collab = None
            for c in self.registry.list_collaborations():
                if c.collaboration_id == collaboration_id:
                    collab = c
                    break
            if not collab:
                raise KeyError(f"Collaboration assignment '{collaboration_id}' not found")

            # Check authority: Worker cannot finalize mission or declare complete
            if attempted_mission_completion:
                raise CollaborationAuthorityError(
                    f"Agent with role {collab.role.value} cannot finalize mission or complete GoalRun (WORKER_MISSION_FINALIZE=0)"
                )

            collab.result_evidence = list(evidence_refs)
            collab.status = "COMPLETED"
            self.registry.save_collaboration(collab)
            return collab

    def submit_reviewer_recommendation(
        self,
        collaboration_id: str,
        recommendation: str,  # "ACCEPT" or "REJECT"
        critique: str = "",
        defects: list[str] | None = None,
        attempted_direct_completion: bool = False,
    ) -> dict[str, Any]:
        """Record reviewer evaluation. Strictly forbids reviewer direct completion (REVIEWER_COMPLETION_AUTHORITY=0)."""
        with self._lock:
            collab = None
            for c in self.registry.list_collaborations():
                if c.collaboration_id == collaboration_id:
                    collab = c
                    break
            if not collab:
                raise KeyError(f"Collaboration assignment '{collaboration_id}' not found")

            # Check authority: Reviewer cannot directly complete or seal mission
            if attempted_direct_completion:
                raise CollaborationAuthorityError(
                    "Reviewer agent cannot directly complete mission (REVIEWER_COMPLETION_AUTHORITY=0)"
                )

            collab.status = "REVIEWED"
            self.registry.save_collaboration(collab)

            return {
                "collaboration_id": collaboration_id,
                "mission_id": collab.mission_id,
                "reviewer_agent": collab.agent_instance_id,
                "recommendation": recommendation,
                "critique": critique,
                "defects": defects or [],
                "semantic_authority": "MasterAgent",
            }

    def handoff_mission(
        self,
        mission_id: str,
        source_agent: str,
        target_agent: str,
        reason: str,
        session_envelope_ref: str,
        accepted_progress_ref: str,
        workspace_ref: str,
        target_capabilities: list[str] | None = None,
    ) -> AgentHandoff:
        """Cross-agent handoff of execution context using SessionEnvelope (spec §35, §36)."""
        with self._lock:
            # Precondition validation: target agent must exist and have required capabilities
            target = self.registry.get_agent(target_agent)
            if not target:
                raise KeyError(f"Target agent '{target_agent}' not found for handoff")

            if target_capabilities and not all(
                c in target.capability_profile for c in target_capabilities
            ):
                raise CollaborationAuthorityError(
                    f"Target agent '{target_agent}' lacks required capabilities for handoff: {target_capabilities}"
                )

            handoff_id = f"handoff_{uuid.uuid4().hex[:12]}"
            handoff = AgentHandoff(
                handoff_id=handoff_id,
                mission_id=mission_id,
                source_agent=source_agent,
                target_agent=target_agent,
                reason=reason,
                session_envelope_ref=session_envelope_ref,
                accepted_progress_ref=accepted_progress_ref,
                workspace_ref=workspace_ref,
                created_at=time.time(),
            )
            return handoff
