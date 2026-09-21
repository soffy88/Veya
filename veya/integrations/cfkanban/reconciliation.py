"""Recovery of provider receipts without synthesizing Mission acceptance."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from veya.providers.cfkanban.ledger import CfKanbanOperationLedger, SideEffectState
from veya.providers.cfkanban.models import CfKanbanTask
from veya.supervision.store import MissionStore

from .binding import BindingState, CfKanbanBindingStore, IssueMissionBinding


class ReadProvider(Protocol):
    def get_issue(self, identifier: str, **kwargs: object) -> CfKanbanTask: ...


@dataclass(frozen=True)
class ReconciliationResult:
    state: str
    binding: IssueMissionBinding
    provider_done: bool


def reconcile_ambiguous_completion(
    binding: IssueMissionBinding,
    *,
    store: MissionStore,
    provider: ReadProvider,
    bindings: CfKanbanBindingStore,
    operation_id: str,
) -> ReconciliationResult:
    ledger = CfKanbanOperationLedger(store, binding.mission_id)
    issue = provider.get_issue(binding.issue_id)
    if issue.state == "done":
        ledger.mark(
            operation_id,
            SideEffectState.CONFIRMED,
            receipt={"provider_task": issue.identifier, "version": issue.version},
        )
        updated = bindings.transition(
            binding,
            BindingState.COMPLETED,
            last_seen_issue_version=issue.version,
        )
        return ReconciliationResult("COMMITTED", updated, True)
    ledger.mark(operation_id, SideEffectState.AMBIGUOUS)
    return ReconciliationResult("REMAINS_AMBIGUOUS", binding, False)


def recover_version_conflict(
    binding: IssueMissionBinding,
    *,
    provider: ReadProvider,
    bindings: CfKanbanBindingStore,
) -> tuple[IssueMissionBinding, bool]:
    """Refresh and report material change; never performs blind retry."""
    current = provider.get_issue(binding.issue_id)
    changed = current.version != binding.last_seen_issue_version
    updated = bindings.transition(
        binding,
        binding.state,
        last_seen_issue_version=current.version,
    )
    return updated, changed
