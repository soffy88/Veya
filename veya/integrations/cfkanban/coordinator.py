"""Small composition facade; execution and routing remain Veya-owned."""

from __future__ import annotations

from collections.abc import Iterable

from veya.providers.cfkanban.models import CfKanbanTask
from veya.supervision.models import Mission
from veya.supervision.store import MissionStore

from .binding import CfKanbanBindingStore, IssueMissionBinding
from .intake import CfKanbanIntake, IntakeResult
from .writeback import CfKanbanWriteback, WriteProvider


class CfKanbanCoordinator:
    """Compose intake/writeback without becoming a Mission runtime or router."""

    def __init__(self, store: MissionStore, provider: WriteProvider) -> None:
        bindings = CfKanbanBindingStore(store)
        self.intake = CfKanbanIntake(store, bindings)
        self.writeback = CfKanbanWriteback(store, provider, bindings)

    def intake_issue(
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
        return self.intake.ingest(
            instance_id=instance_id,
            issue=issue,
            event_id=event_id,
            event_cursor=event_cursor,
            authorized_project_ids=authorized_project_ids,
            supervision_mode=supervision_mode,
            autonomy_level=autonomy_level,
        )

    def mark_started(self, binding: IssueMissionBinding) -> IssueMissionBinding:
        return self.writeback.started(binding)

    def complete_verified(
        self,
        binding: IssueMissionBinding,
        mission: Mission,
        *,
        evidence: list[str],
        result: str,
    ) -> IssueMissionBinding:
        return self.writeback.complete(
            binding,
            mission,
            verification_pass=True,
            evidence=evidence,
            unresolved_failures=[],
            result=result,
        )
