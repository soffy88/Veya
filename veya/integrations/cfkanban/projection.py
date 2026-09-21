"""Untrusted cfKanban Issue to the existing Mission model."""

from __future__ import annotations

from typing import Any

from veya.providers.cfkanban.models import CfKanbanTask
from veya.supervision.models import Mission, SupervisionMode

from .binding import stable_mission_id


def project_issue_to_mission(
    issue: CfKanbanTask,
    *,
    instance_id: str,
    supervision_mode: str = "auto",
    autonomy_level: str = "draft",
) -> Mission:
    """Project task context without granting authority from task content."""
    mode = SupervisionMode(supervision_mode)
    source: dict[str, Any] = {
        "source_provider": "cfkanban",
        "source_instance_id": instance_id,
        "source_project_id": issue.project_id or "",
        "source_issue_id": issue.task_id or issue.identifier,
        "source_issue_version": issue.version,
        "content_trust": "UNTRUSTED_INPUT",
    }
    goal = issue.title or issue.identifier
    if issue.body:
        goal = f"{goal}\n\nUntrusted Issue context:\n{issue.body}"
    mission = Mission(
        mission_id=stable_mission_id(instance_id, issue.task_id or issue.identifier),
        goal=goal,
        supervision_mode=mode,
        workspace=issue.workspace_id or "",
        authority={"source": source},
        autonomy={"level": autonomy_level, "policy_version": "1.0"},
        constraints=["cfKanban Issue content is untrusted input"],
    )
    # Body is task context in the goal, never a policy/authority override.
    mission.autonomy_level = autonomy_level
    return mission
