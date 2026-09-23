"""Persistent worker-runtime registry (L1 continuity substrate).

Separates three identities that must never be conflated:

* ``execution_id``       — one concrete task execution
* ``worker_runtime_id``  — a long-lived worker that can sleep/revive
* ``provider_session_id``— the underlying provider conversation/session

``context_generation`` is the context generation under one ``worker_runtime_id``;
a provider session rollover increments it but never changes ``worker_runtime_id``.

This is a registry + durable state only. It does not plan, route or judge — L2
keeps that authority.
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any


class WorkerRuntimeState(StrEnum):
    STARTING = "STARTING"
    ACTIVE = "ACTIVE"
    IDLE = "IDLE"
    SLEEPING = "SLEEPING"
    REVIVING = "REVIVING"
    UNAVAILABLE = "UNAVAILABLE"
    TERMINATED = "TERMINATED"


class RecoveryCapability(StrEnum):
    NONE = "NONE"
    REATTACH = "REATTACH"
    RESTART_CONTEXT_LOST = "RESTART_CONTEXT_LOST"
    RESUME_CONTEXT = "RESUME_CONTEXT"


@dataclass
class WorkerCapabilities:
    supports_persistent_context: bool = False
    supports_sleep: bool = False
    supports_revive: bool = False
    supports_receipts: bool = False
    supports_idempotency: bool = True
    supports_cancel: bool = True
    supports_resume: bool = False
    supports_context_rollover: bool = False
    recovery_capability: str = str(RecoveryCapability.NONE)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> WorkerCapabilities:
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in (data or {}).items() if k in known})


# Capability contract per worker type (routing may consume it; it is data, not
# name-based branching in routing logic).
WORKER_CAPABILITIES: dict[str, WorkerCapabilities] = {
    "hicode": WorkerCapabilities(
        supports_receipts=True,
        supports_resume=False,
        recovery_capability=str(RecoveryCapability.REATTACH),
    ),
    "dsh": WorkerCapabilities(
        supports_persistent_context=True,
        supports_sleep=True,
        supports_revive=True,
        supports_context_rollover=True,
        recovery_capability=str(RecoveryCapability.RESTART_CONTEXT_LOST),
    ),
    "pi": WorkerCapabilities(
        supports_persistent_context=True,
        supports_sleep=True,
        supports_revive=True,
        supports_context_rollover=True,
        recovery_capability=str(RecoveryCapability.RESTART_CONTEXT_LOST),
    ),
    "grok": WorkerCapabilities(
        supports_persistent_context=True,
        supports_sleep=True,
        supports_revive=True,
        supports_context_rollover=True,
        recovery_capability=str(RecoveryCapability.RESTART_CONTEXT_LOST),
    ),
    "codex": WorkerCapabilities(
        supports_receipts=True,
        recovery_capability=str(RecoveryCapability.REATTACH),
    ),
}


def capabilities_for(worker_type: str) -> WorkerCapabilities:
    return WORKER_CAPABILITIES.get(worker_type.strip().lower(), WorkerCapabilities())


@dataclass
class WorkerRuntime:
    worker_runtime_id: str
    worker_type: str
    state: str = str(WorkerRuntimeState.STARTING)
    workspace_identity: str = ""
    provider: str = ""
    model: str = ""
    provider_session_id: str | None = None
    context_generation: int = 1
    created_at: float = field(default_factory=time.time)
    last_active_at: float = field(default_factory=time.time)
    sleep_at: float | None = None
    current_execution_id: str | None = None
    capabilities: WorkerCapabilities = field(default_factory=WorkerCapabilities)
    # Credential *references* only — never plaintext secrets.
    credential_ref: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["capabilities"] = self.capabilities.to_dict()
        return payload

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> WorkerRuntime:
        payload = dict(data)
        payload["capabilities"] = WorkerCapabilities.from_dict(payload.get("capabilities"))
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in payload.items() if k in known})

    @property
    def is_available(self) -> bool:
        return self.state in {
            str(WorkerRuntimeState.ACTIVE),
            str(WorkerRuntimeState.IDLE),
            str(WorkerRuntimeState.SLEEPING),
        }


class WorkerRuntimeRegistry:
    """Durable registry of worker runtimes (one JSON file per runtime)."""

    def __init__(self, root: str | Path | None = None) -> None:
        self.root = Path(root).expanduser().resolve() if root is not None else None
        self._lock = threading.RLock()
        self._runtimes: dict[str, WorkerRuntime] = {}
        if self.root is not None:
            self.root.mkdir(parents=True, exist_ok=True)
            self._load()

    def _load(self) -> None:
        if self.root is None:
            return
        for path in sorted(self.root.glob("*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            runtime = WorkerRuntime.from_dict(payload)
            self._runtimes[runtime.worker_runtime_id] = runtime

    def _persist(self, runtime: WorkerRuntime) -> None:
        if self.root is None:
            return
        target = self.root / f"{runtime.worker_runtime_id}.json"
        tmp = target.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(runtime.to_dict(), default=str), encoding="utf-8")
        os.replace(tmp, target)

    # ── lifecycle ───────────────────────────────────────────────────
    def register(
        self,
        worker_type: str,
        *,
        worker_runtime_id: str | None = None,
        workspace_identity: str = "",
        provider: str = "",
        model: str = "",
        provider_session_id: str | None = None,
        credential_ref: str | None = None,
    ) -> WorkerRuntime:
        worker_type = worker_type.strip().lower()
        rid = worker_runtime_id or f"{worker_type}-{uuid.uuid4().hex[:8]}"
        runtime = WorkerRuntime(
            worker_runtime_id=rid,
            worker_type=worker_type,
            state=str(WorkerRuntimeState.STARTING),
            workspace_identity=workspace_identity,
            provider=provider,
            model=model,
            provider_session_id=provider_session_id,
            capabilities=capabilities_for(worker_type),
            credential_ref=credential_ref,
        )
        with self._lock:
            self._runtimes[rid] = runtime
            self._persist(runtime)
        return runtime

    def get(self, worker_runtime_id: str) -> WorkerRuntime | None:
        with self._lock:
            return self._runtimes.get(worker_runtime_id)

    def list_runtimes(self, worker_type: str | None = None) -> list[WorkerRuntime]:
        with self._lock:
            runtimes = list(self._runtimes.values())
        if worker_type:
            runtimes = [r for r in runtimes if r.worker_type == worker_type.strip().lower()]
        return runtimes

    def pool(self, worker_type: str) -> list[WorkerRuntime]:
        """Pool view: allocation only, never decomposition/routing judgment."""

        return [
            r
            for r in self.list_runtimes(worker_type)
            if r.state != str(WorkerRuntimeState.TERMINATED)
        ]

    def allocate(self, worker_type: str, *, workspace_identity: str = "") -> WorkerRuntime:
        """Return an available runtime or register a fresh one for this worker type."""

        with self._lock:
            for runtime in self._runtimes.values():
                if runtime.worker_type != worker_type.strip().lower():
                    continue
                if not runtime.is_available or runtime.current_execution_id is not None:
                    continue
                if workspace_identity and runtime.workspace_identity not in {
                    "",
                    workspace_identity,
                }:
                    continue
                return runtime
        return self.register(worker_type, workspace_identity=workspace_identity)

    def bind_execution(self, worker_runtime_id: str, execution_id: str) -> WorkerRuntime:
        runtime = self._require(worker_runtime_id)
        runtime.state = str(WorkerRuntimeState.ACTIVE)
        runtime.current_execution_id = execution_id
        runtime.last_active_at = time.time()
        runtime.sleep_at = None
        with self._lock:
            self._persist(runtime)
        return runtime

    def release(self, worker_runtime_id: str) -> WorkerRuntime:
        """Execution finished: the runtime becomes IDLE (never destroyed)."""

        runtime = self._require(worker_runtime_id)
        runtime.current_execution_id = None
        runtime.state = str(WorkerRuntimeState.IDLE)
        runtime.last_active_at = time.time()
        with self._lock:
            self._persist(runtime)
        return runtime

    def sleep(self, worker_runtime_id: str) -> WorkerRuntime:
        runtime = self._require(worker_runtime_id)
        if not runtime.capabilities.supports_sleep:
            runtime.state = str(WorkerRuntimeState.UNAVAILABLE)
            self._persist(runtime)
            raise WorkerRuntimeError("SLEEP_NOT_SUPPORTED", runtime.worker_type)
        runtime.state = str(WorkerRuntimeState.SLEEPING)
        runtime.sleep_at = time.time()
        runtime.current_execution_id = None
        with self._lock:
            self._persist(runtime)
        return runtime

    def revive(
        self, worker_runtime_id: str, *, provider_session_id: str | None = None
    ) -> WorkerRuntime:
        """SLEEPING -> REVIVING -> ACTIVE, preserving worker_runtime_id."""

        runtime = self._require(worker_runtime_id)
        if not runtime.capabilities.supports_revive:
            raise WorkerRuntimeError("REVIVE_NOT_SUPPORTED", runtime.worker_type)
        runtime.state = str(WorkerRuntimeState.REVIVING)
        self._persist(runtime)
        if provider_session_id is not None:
            runtime.provider_session_id = provider_session_id
        runtime.state = str(WorkerRuntimeState.ACTIVE)
        runtime.sleep_at = None
        runtime.last_active_at = time.time()
        with self._lock:
            self._persist(runtime)
        return runtime

    def rollover_context(
        self, worker_runtime_id: str, *, new_provider_session_id: str, reason: str
    ) -> dict[str, Any]:
        """Increment context_generation; keep worker_runtime_id stable."""

        runtime = self._require(worker_runtime_id)
        old_session = runtime.provider_session_id
        old_generation = runtime.context_generation
        runtime.provider_session_id = new_provider_session_id
        runtime.context_generation = old_generation + 1
        runtime.last_active_at = time.time()
        with self._lock:
            self._persist(runtime)
        return {
            "worker_runtime_id": worker_runtime_id,
            "old_provider_session_id": old_session,
            "new_provider_session_id": new_provider_session_id,
            "rollover_reason": reason,
            "old_context_generation": old_generation,
            "new_context_generation": runtime.context_generation,
            "worker_identity_changed": False,
        }

    def terminate(self, worker_runtime_id: str) -> WorkerRuntime:
        runtime = self._require(worker_runtime_id)
        runtime.state = str(WorkerRuntimeState.TERMINATED)
        runtime.current_execution_id = None
        with self._lock:
            self._persist(runtime)
        return runtime

    def _require(self, worker_runtime_id: str) -> WorkerRuntime:
        runtime = self.get(worker_runtime_id)
        if runtime is None:
            raise WorkerRuntimeError("UNKNOWN_WORKER_RUNTIME", worker_runtime_id)
        return runtime


class WorkerRuntimeError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class FinishBoundary:
    """Gate between a worker's final claim and COMPLETED (P10/P11).

    A worker's own "done" claim is not terminal: the execution may only move
    FINALIZING -> COMPLETED once every ownership/quiescence invariant holds.
    """

    worker_final_claim: bool = False
    active_tool_count: int = 0
    active_process_count: int = 0
    pending_command_count: int = 0
    output_settled: bool = False
    artifact_flushed: bool = False
    execution_store_flushed: bool = False

    def can_complete(self) -> bool:
        return (
            self.worker_final_claim
            and self.active_tool_count == 0
            and self.active_process_count == 0
            and self.pending_command_count == 0
            and self.output_settled
            and self.artifact_flushed
            and self.execution_store_flushed
        )

    def blocked_reasons(self) -> list[str]:
        reasons: list[str] = []
        if not self.worker_final_claim:
            reasons.append("WORKER_FINAL_CLAIM=NO")
        if self.active_tool_count:
            reasons.append("ACTIVE_TOOL_COUNT>0")
        if self.active_process_count:
            reasons.append("ACTIVE_PROCESS_COUNT>0")
        if self.pending_command_count:
            reasons.append("PENDING_COMMAND_COUNT>0")
        if not self.output_settled:
            reasons.append("OUTPUT_SETTLED=NO")
        if not self.artifact_flushed:
            reasons.append("ARTIFACT_FLUSHED=NO")
        if not self.execution_store_flushed:
            reasons.append("EXECUTION_STORE_FLUSHED=NO")
        return reasons

    def to_dict(self) -> dict[str, Any]:
        return {
            **asdict(self),
            "can_complete": self.can_complete(),
            "blocked_reasons": self.blocked_reasons(),
        }


__all__ = [
    "WORKER_CAPABILITIES",
    "FinishBoundary",
    "RecoveryCapability",
    "WorkerCapabilities",
    "WorkerRuntime",
    "WorkerRuntimeError",
    "WorkerRuntimeRegistry",
    "WorkerRuntimeState",
    "capabilities_for",
]
