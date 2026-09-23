"""Durable command outbox with idempotency and side-effect truth.

Every important command sent to a long-lived worker/provider is persisted
*before* it is sent. Delivery truth is layered so ``send() == ok`` never means
``worker completed``. A lost acknowledgement becomes ``AMBIGUOUS`` and is never
auto-replayed; reconciliation resolves it from evidence.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any


class CommandState(StrEnum):
    CREATED = "CREATED"
    CLAIMED = "CLAIMED"
    SENDING = "SENDING"
    DELIVERED = "DELIVERED"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    AMBIGUOUS = "AMBIGUOUS"
    CANCELLED = "CANCELLED"


class SideEffectState(StrEnum):
    NOT_STARTED = "NOT_STARTED"
    PENDING = "PENDING"
    CONFIRMED = "CONFIRMED"
    FAILED = "FAILED"
    AMBIGUOUS = "AMBIGUOUS"


_TERMINAL = {str(CommandState.COMPLETED), str(CommandState.FAILED), str(CommandState.CANCELLED)}


class OutboxError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class Command:
    command_id: str
    idempotency_key: str
    execution_id: str
    worker_runtime_id: str
    payload_hash: str
    state: str = str(CommandState.CREATED)
    side_effect_state: str = str(SideEffectState.NOT_STARTED)
    attempt: int = 0
    created_at: float = field(default_factory=time.time)
    claimed_at: float | None = None
    delivered_at: float | None = None
    acknowledged_at: float | None = None
    completed_at: float | None = None
    last_error: str | None = None
    result_ref: str | None = None
    provider_request_id: str | None = None

    @property
    def is_terminal(self) -> bool:
        return self.state in _TERMINAL

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Command:
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})


def payload_hash(payload: Any) -> str:
    blob = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


class DurableOutbox:
    def __init__(self, root: str | Path | None = None) -> None:
        self.root = Path(root).expanduser().resolve() if root is not None else None
        self._lock = threading.RLock()
        self._commands: dict[str, Command] = {}
        self._by_key: dict[str, str] = {}
        if self.root is not None:
            self.root.mkdir(parents=True, exist_ok=True)
            self._load()

    def _load(self) -> None:
        if self.root is None:
            return
        for path in sorted(self.root.glob("cmd_*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            command = Command.from_dict(payload)
            self._commands[command.command_id] = command
            self._by_key[command.idempotency_key] = command.command_id

    def _persist(self, command: Command) -> None:
        if self.root is None:
            return
        target = self.root / f"{command.command_id}.json"
        tmp = target.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(command.to_dict(), default=str), encoding="utf-8")
        os.replace(tmp, target)

    def get(self, command_id: str) -> Command | None:
        with self._lock:
            return self._commands.get(command_id)

    def by_key(self, idempotency_key: str) -> Command | None:
        with self._lock:
            cid = self._by_key.get(idempotency_key)
            return self._commands.get(cid) if cid else None

    def create(
        self,
        *,
        idempotency_key: str,
        execution_id: str,
        worker_runtime_id: str,
        payload: Any,
    ) -> tuple[Command, bool]:
        """Persist the intent before sending. Returns ``(command, created)``."""

        with self._lock:
            existing = self.by_key(idempotency_key)
            if existing is not None:
                return existing, False
            command = Command(
                command_id=f"cmd_{uuid.uuid4().hex}",
                idempotency_key=idempotency_key,
                execution_id=execution_id,
                worker_runtime_id=worker_runtime_id,
                payload_hash=payload_hash(payload),
            )
            self._commands[command.command_id] = command
            self._by_key[idempotency_key] = command.command_id
            self._persist(command)
            return command, True

    def _update(self, command_id: str, **fields: Any) -> Command:
        with self._lock:
            command = self._commands.get(command_id)
            if command is None:
                raise OutboxError("UNKNOWN_COMMAND", command_id)
            for key, value in fields.items():
                setattr(command, key, value)
            self._persist(command)
            return command

    def claim(self, command_id: str) -> Command:
        command = self._require(command_id)
        if command.state != str(CommandState.CREATED):
            return command
        return self._update(
            command_id,
            state=str(CommandState.CLAIMED),
            claimed_at=time.time(),
            attempt=command.attempt + 1,
        )

    def mark_sent(self, command_id: str) -> Command:
        return self._update(command_id, state=str(CommandState.SENDING))

    def mark_delivered(self, command_id: str, *, provider_request_id: str | None = None) -> Command:
        return self._update(
            command_id,
            state=str(CommandState.DELIVERED),
            delivered_at=time.time(),
            provider_request_id=provider_request_id,
            side_effect_state=str(SideEffectState.PENDING),
        )

    def mark_acknowledged(self, command_id: str) -> Command:
        return self._update(
            command_id,
            state=str(CommandState.ACKNOWLEDGED),
            acknowledged_at=time.time(),
            side_effect_state=str(SideEffectState.PENDING),
        )

    def mark_completed(self, command_id: str, *, result_ref: str | None = None) -> Command:
        return self._update(
            command_id,
            state=str(CommandState.COMPLETED),
            side_effect_state=str(SideEffectState.CONFIRMED),
            completed_at=time.time(),
            result_ref=result_ref,
        )

    def mark_failed(self, command_id: str, *, error: str) -> Command:
        return self._update(
            command_id,
            state=str(CommandState.FAILED),
            side_effect_state=str(SideEffectState.FAILED),
            last_error=error,
        )

    def mark_ambiguous(self, command_id: str, *, error: str) -> Command:
        """Acknowledgement lost / connection dropped: true effect unknown."""

        return self._update(
            command_id,
            state=str(CommandState.AMBIGUOUS),
            side_effect_state=str(SideEffectState.AMBIGUOUS),
            last_error=error,
        )

    def reconcile(self, command_id: str, *, confirmed: bool) -> Command:
        """Resolve an AMBIGUOUS command from evidence (never a blind replay)."""

        command = self._require(command_id)
        if command.state != str(CommandState.AMBIGUOUS):
            return command
        if confirmed:
            return self.mark_completed(command_id)
        return self.mark_failed(command_id, error="reconciled: not applied")

    def can_replay(self, command_id: str) -> bool:
        command = self._require(command_id)
        # Never blind-replay an ambiguous side effect.
        return command.state not in {
            str(CommandState.AMBIGUOUS)
        } and command.side_effect_state != str(SideEffectState.AMBIGUOUS)

    def pending_count(self, worker_runtime_id: str) -> int:
        with self._lock:
            return sum(
                1
                for c in self._commands.values()
                if c.worker_runtime_id == worker_runtime_id and not c.is_terminal
            )

    def _require(self, command_id: str) -> Command:
        command = self.get(command_id)
        if command is None:
            raise OutboxError("UNKNOWN_COMMAND", command_id)
        return command


__all__ = [
    "Command",
    "CommandState",
    "DurableOutbox",
    "OutboxError",
    "SideEffectState",
    "payload_hash",
]
