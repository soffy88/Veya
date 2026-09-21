"""At-least-once event intake and deterministic binding creation."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from veya.providers.cfkanban.models import CfKanbanTask
from veya.supervision.models import MissionStatus
from veya.supervision.store import MissionStore

from .binding import (
    ACTIVE_STATES,
    BindingState,
    CfKanbanBindingStore,
    IssueMissionBinding,
    stable_mission_id,
)
from .projection import project_issue_to_mission


@dataclass(frozen=True)
class IntakeResult:
    action: str
    binding: IssueMissionBinding | None
    mission_id: str | None


class CfKanbanIntake:
    def __init__(self, store: MissionStore, bindings: CfKanbanBindingStore | None = None) -> None:
        self.store = store
        self.bindings = bindings or CfKanbanBindingStore(store)

    def ingest(
        self,
        *,
        instance_id: str,
        issue: CfKanbanTask,
        event_id: str,
        event_cursor: str | None,
        authorized_project_ids: Iterable[str],
        supervision_mode: str = "auto",
        autonomy_level: str = "draft",
    ) -> IntakeResult:
        authorized_projects = set(authorized_project_ids)
        scope = f"{instance_id}:{','.join(sorted(authorized_projects))}"
        if self.bindings.event_committed(scope, event_id):
            binding = self.bindings.find(instance_id, issue.task_id or issue.identifier)
            return IntakeResult("REPLAY", binding, binding.mission_id if binding else None)

        issue_id = issue.task_id or issue.identifier
        existing = self.bindings.find(instance_id, issue_id)
        if issue.state == "done":
            binding = existing
            if binding is not None and binding.state in ACTIVE_STATES:
                binding = self.bindings.transition(binding, BindingState.DETACHED)
            self.bindings.commit_event(scope, event_id, event_cursor)
            return IntakeResult(
                "EXTERNALLY_COMPLETED", binding, binding.mission_id if binding else None
            )

        authorized = bool(issue.project_id and issue.project_id in authorized_projects)
        eligible = authorized and not issue.blocked
        if not eligible:
            self.bindings.commit_event(scope, event_id, event_cursor)
            return IntakeResult("IGNORED", existing, existing.mission_id if existing else None)

        if existing is not None and existing.state in ACTIVE_STATES:
            updated = self.bindings.transition(
                existing,
                existing.state,
                last_seen_issue_version=issue.version,
                last_event_cursor=event_cursor,
            )
            self.bindings.commit_event(scope, event_id, event_cursor)
            return IntakeResult("REUSED", updated, updated.mission_id)

        mission_id = stable_mission_id(instance_id, issue_id)
        mission = self.store.load(mission_id)
        if mission is None:
            mission = project_issue_to_mission(
                issue,
                instance_id=instance_id,
                supervision_mode=supervision_mode,
                autonomy_level=autonomy_level,
            )
            self.store.save(mission)
            self.store.append_event(mission_id, "MISSION_CREATED", {"source": "cfkanban"})
        elif mission.status in {MissionStatus.accepted, MissionStatus.done}:
            self.bindings.commit_event(scope, event_id, event_cursor)
            return IntakeResult("MISSION_ALREADY_ACCEPTED", existing, mission_id)

        now = mission.updated_at
        binding = IssueMissionBinding(
            provider="cfkanban",
            instance_id=instance_id,
            workspace_id=issue.workspace_id or "",
            project_id=issue.project_id or "",
            issue_id=issue_id,
            issue_number=issue.number,
            mission_id=mission_id,
            issue_version_at_bind=issue.version,
            last_seen_issue_version=issue.version,
            last_event_cursor=event_cursor,
            supervision_mode=str(mission.supervision_mode),
            autonomy_level=mission.autonomy_level,
            state=BindingState.BOUND,
            created_at=now,
            updated_at=now,
        )
        self.bindings.save(binding)
        self.bindings.commit_event(scope, event_id, event_cursor)
        return IntakeResult("BOUND", binding, mission_id)
