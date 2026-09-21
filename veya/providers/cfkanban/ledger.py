"""Provider side-effect records on the canonical MissionStore boundary."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, cast

from veya.supervision.store import MissionStore

from .errors import CfKanbanErrorClass, CfKanbanProviderError


class SideEffectState(StrEnum):
    NOT_STARTED = "NOT_STARTED"
    PENDING = "PENDING"
    CONFIRMED = "CONFIRMED"
    FAILED = "FAILED"
    AMBIGUOUS = "AMBIGUOUS"


@dataclass(frozen=True)
class DurableProviderOperation:
    operation_id: str
    mission_id: str
    execution_id: str
    operation: str
    idempotency_key: str
    state: SideEffectState
    receipt: dict[str, Any] | None = None


class CfKanbanOperationLedger:
    """Append-only provider receipt ledger backed by the existing MissionStore."""

    def __init__(self, store: MissionStore, mission_id: str) -> None:
        self.store = store
        self.mission_id = mission_id

    def _records(self) -> list[dict[str, Any]]:
        records = cast(list[dict[str, Any]], self.store.executions(self.mission_id))
        return [record for record in records if record.get("provider") == "cfkanban"]

    def find(self, operation_id: str) -> DurableProviderOperation | None:
        current: dict[str, Any] | None = None
        for record in self._records():
            if record.get("operation_id") == operation_id:
                current = {**(current or {}), **record}
        if current is None:
            return None
        return DurableProviderOperation(
            operation_id=operation_id,
            mission_id=self.mission_id,
            execution_id=str(current.get("execution_id", "")),
            operation=str(current.get("operation", "")),
            idempotency_key=str(current.get("idempotency_key", "")),
            state=SideEffectState(str(current.get("side_effect_state", "NOT_STARTED"))),
            receipt=current.get("receipt") if isinstance(current.get("receipt"), dict) else None,
        )

    def begin(
        self,
        operation_id: str,
        execution_id: str,
        operation: str,
        idempotency_key: str,
    ) -> DurableProviderOperation:
        existing = self.find(operation_id)
        if existing is not None:
            return existing
        self.store.append_execution(
            self.mission_id,
            {
                "provider": "cfkanban",
                "operation_id": operation_id,
                "execution_id": execution_id,
                "operation": operation,
                "idempotency_key": idempotency_key,
                "side_effect_state": SideEffectState.PENDING.value,
            },
        )
        return self.find(operation_id)  # type: ignore[return-value]

    def mark(
        self,
        operation_id: str,
        state: SideEffectState,
        *,
        receipt: dict[str, Any] | None = None,
    ) -> None:
        self.store.append_execution(
            self.mission_id,
            {
                "provider": "cfkanban",
                "operation_id": operation_id,
                "side_effect_state": state.value,
                **({"receipt": receipt} if receipt is not None else {}),
            },
        )

    def save_cursor(self, cursor_id: str, value: str | None) -> None:
        self.store.append_execution(
            self.mission_id,
            {
                "provider": "cfkanban",
                "cursor_id": cursor_id,
                "cursor": value,
                "cursor_durable": True,
            },
        )

    def cursor(self, cursor_id: str) -> str | None:
        value: str | None = None
        for record in self._records():
            if record.get("cursor_id") == cursor_id and record.get("cursor_durable"):
                value = record.get("cursor")
        return value

    def persist_events_then_advance(
        self, cursor_id: str, events: list[dict[str, Any]], next_cursor: str | None
    ) -> None:
        """Persist normalized events before recording the returned cursor."""
        existing_ids = {
            str(record.get("event_id"))
            for record in self._records()
            if record.get("event_id") is not None
        }
        for event in events:
            if str(event["event_id"]) in existing_ids:
                continue
            self.store.append_execution(
                self.mission_id,
                {"provider": "cfkanban", "event_id": event["event_id"], "event": event},
            )
            existing_ids.add(str(event["event_id"]))
        self.save_cursor(cursor_id, next_cursor)

    def execute(
        self,
        operation_id: str,
        execution_id: str,
        operation: str,
        idempotency_key: str,
        call: Any,
    ) -> Any:
        """Run one mutation across the durable intent/receipt boundary.

        A transport ambiguity is quarantined and never automatically replayed.
        The callable is deliberately injected so the ledger does not become a
        second provider client or runtime.
        """
        prior = self.find(operation_id)
        if prior is not None and prior.state == SideEffectState.PENDING:
            self.mark(operation_id, SideEffectState.AMBIGUOUS)
            raise CfKanbanProviderError(
                CfKanbanErrorClass.AMBIGUOUS,
                "cfKanban operation was pending after restart; reconcile before replay",
                provider_code="PENDING_OPERATION_REQUIRES_RECONCILIATION",
            )
        existing = prior or self.begin(operation_id, execution_id, operation, idempotency_key)
        if existing.state in {SideEffectState.CONFIRMED, SideEffectState.AMBIGUOUS}:
            return existing
        try:
            result = call()
        except Exception as exc:
            if isinstance(exc, CfKanbanProviderError) and exc.canonical_class.value == "AMBIGUOUS":
                self.mark(operation_id, SideEffectState.AMBIGUOUS)
            else:
                self.mark(operation_id, SideEffectState.FAILED)
            raise
        receipt = result.to_dict() if hasattr(result, "to_dict") else {"result": result}
        self.mark(operation_id, SideEffectState.CONFIRMED, receipt=receipt)
        return result
