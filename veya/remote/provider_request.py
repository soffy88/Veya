"""Independent provider-request lifecycle for a durable Execution."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
import time
import uuid


class ProviderRequestStatus(StrEnum):
    REQUESTED = "REQUESTED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    TIMED_OUT = "TIMED_OUT"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


_TERMINAL = frozenset({
    ProviderRequestStatus.COMPLETED,
    ProviderRequestStatus.TIMED_OUT,
    ProviderRequestStatus.FAILED,
    ProviderRequestStatus.CANCELLED,
})

_ALLOWED = {
    ProviderRequestStatus.REQUESTED: frozenset({ProviderRequestStatus.RUNNING, ProviderRequestStatus.CANCELLED}),
    ProviderRequestStatus.RUNNING: frozenset(_TERMINAL),
}


@dataclass
class ProviderRequest:
    """A provider call is a child resource, not the Execution itself."""

    execution_id: str
    goal_run_id: str | None = None
    provider: str | None = None
    model: str | None = None
    request_id: str = field(default_factory=lambda: f"preq_{uuid.uuid4().hex}")
    status: ProviderRequestStatus = ProviderRequestStatus.REQUESTED
    timeout_ms: int | None = None
    started_at: float | None = None
    completed_at: float | None = None
    error_code: str | None = None

    def transition(self, status: ProviderRequestStatus, *, error_code: str | None = None, now: float | None = None) -> None:
        if self.status in _TERMINAL:
            if status == self.status:
                return
            raise ValueError(f"provider request {self.request_id} is terminal: {self.status}")
        if status not in _ALLOWED.get(self.status, frozenset()):
            raise ValueError(f"invalid provider request transition {self.status} -> {status}")
        stamp = time.time() if now is None else float(now)
        if status is ProviderRequestStatus.RUNNING:
            self.started_at = stamp
        if status in _TERMINAL:
            self.completed_at = stamp
        self.status = status
        if error_code is not None:
            self.error_code = str(error_code)

    @property
    def is_terminal(self) -> bool:
        return self.status in _TERMINAL

    @property
    def elapsed_ms(self) -> int | None:
        if self.started_at is None:
            return None
        end = self.completed_at if self.completed_at is not None else time.time()
        return max(0, int((end - self.started_at) * 1000))

    def timeout(self, *, now: float | None = None, error_code: str = "PROVIDER_TIMEOUT") -> None:
        self.transition(ProviderRequestStatus.TIMED_OUT, error_code=error_code, now=now)
