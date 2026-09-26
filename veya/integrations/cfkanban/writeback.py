"""Meaningful, verified-only cfKanban writeback on the existing MissionStore."""

from __future__ import annotations

import re
from typing import Any, Protocol

from veya.providers.cfkanban.ledger import CfKanbanOperationLedger
from veya.providers.cfkanban.mapping import derive_idempotency_key
from veya.providers.cfkanban.models import CfKanbanMutationResult
from veya.supervision.models import Mission, MissionStatus
from veya.supervision.store import MissionStore

from .binding import BindingState, CfKanbanBindingStore, IssueMissionBinding


class WriteProvider(Protocol):
    def create_comment(
        self, identifier: str, body: str, idempotency_key: str, **kwargs: Any
    ) -> CfKanbanMutationResult: ...

    def report_blocked(
        self, identifier: str, expected_version: int, reason: str, idempotency_key: str
    ) -> CfKanbanMutationResult: ...

    def complete_issue(
        self, identifier: str, expected_version: int, idempotency_key: str, **kwargs: Any
    ) -> CfKanbanMutationResult: ...


def _verified_completion(
    mission: Mission,
    *,
    verification_pass: bool,
    evidence: list[str],
    unresolved_failures: list[str],
) -> bool:
    return (
        mission.status in {MissionStatus.accepted, MissionStatus.done}
        and verification_pass
        and bool(evidence)
        and not unresolved_failures
    )


_SECRET_PATTERN = re.compile(
    r"(?i)(authorization|bearer|api[_-]?key|secret|password|cookie)\s*[:=]"
)


def _safe_writeback_text(value: str, *, limit: int) -> str:
    if len(value) > limit:
        raise ValueError("cfKanban writeback exceeds bounded size")
    if _SECRET_PATTERN.search(value):
        raise ValueError("secret-like content cannot be written to cfKanban")
    return value


class CfKanbanWriteback:
    def __init__(
        self,
        store: MissionStore,
        provider: WriteProvider,
        bindings: CfKanbanBindingStore | None = None,
    ) -> None:
        self.store = store
        self.provider = provider
        self.bindings = bindings or CfKanbanBindingStore(store)

    def _ledger(self, binding: IssueMissionBinding) -> CfKanbanOperationLedger:
        return CfKanbanOperationLedger(self.store, binding.mission_id)

    def _comment(
        self, binding: IssueMissionBinding, semantic_event: str, body: str, execution_id: str = ""
    ) -> Any:
        body = _safe_writeback_text(body, limit=4096)
        operation_id = f"comment:{semantic_event}"
        key = derive_idempotency_key(binding.mission_id, execution_id, "comment", semantic_event)
        return self._ledger(binding).execute(
            operation_id,
            execution_id,
            "comment",
            key,
            lambda: self.provider.create_comment(binding.issue_id, body, key),
        )

    def started(
        self, binding: IssueMissionBinding, *, execution_id: str = ""
    ) -> IssueMissionBinding:
        if binding.state != BindingState.RUNNING:
            binding = self.bindings.transition(binding, BindingState.RUNNING)
        self._comment(
            binding, "started", f"Accepted by Veya Mission {binding.mission_id}.", execution_id
        )
        return binding

    def blocked(
        self, binding: IssueMissionBinding, reason: str, *, execution_id: str = ""
    ) -> IssueMissionBinding:
        key = derive_idempotency_key(binding.mission_id, execution_id, "report_blocked", "blocked")
        self._ledger(binding).execute(
            "blocked",
            execution_id,
            "report_blocked",
            key,
            lambda: self.provider.report_blocked(
                binding.issue_id, binding.last_seen_issue_version, reason, key
            ),
        )
        return self.bindings.transition(binding, BindingState.BLOCKED)

    def failed(
        self, binding: IssueMissionBinding, reason: str, *, execution_id: str = ""
    ) -> IssueMissionBinding:
        self._comment(binding, "failed", f"Veya execution failed: {reason[:1000]}", execution_id)
        return self.bindings.transition(binding, BindingState.BLOCKED)

    def complete(
        self,
        binding: IssueMissionBinding,
        mission: Mission,
        *,
        verification_pass: bool,
        evidence: list[str],
        unresolved_failures: list[str],
        result: str,
        execution_id: str = "",
        final_report_id: str = "final",
    ) -> IssueMissionBinding:
        if not _verified_completion(
            mission,
            verification_pass=verification_pass,
            evidence=evidence,
            unresolved_failures=unresolved_failures,
        ):
            raise ValueError("provider completion requires verified Veya ACCEPTED/DONE")
        if binding.state == BindingState.COMPLETED:
            return binding
        result = _safe_writeback_text(result, limit=8192)
        if len(evidence) > 32 or any(len(item) > 1024 for item in evidence):
            raise ValueError("completion evidence is not bounded")
        if any(_SECRET_PATTERN.search(item) for item in evidence):
            raise ValueError("secret-like evidence cannot be written to cfKanban")
        key = derive_idempotency_key(binding.mission_id, execution_id, "complete", final_report_id)
        self._ledger(binding).execute(
            f"complete:{final_report_id}",
            execution_id,
            "complete",
            key,
            lambda: self.provider.complete_issue(
                binding.issue_id,
                binding.last_seen_issue_version,
                key,
                summary=result[:8192],
                verification=evidence,
                artifacts=[{"kind": "other", "value": item} for item in evidence],
            ),
        )
        return self.bindings.transition(binding, BindingState.COMPLETED)
