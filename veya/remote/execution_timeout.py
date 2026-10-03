"""Execution timeout policy: which clock expired, and what it was measured against.

The defect this removes. An ``ExecutionRecord`` already carried three separate
budget fields (``execution_timeout_sec``, ``command_timeout_sec``,
``effective_timeout_ms``) and one answer to "what timed out"
(``timeout_type``/``timeout_seconds``). But the clock was chosen by whichever
writer got there first:

    kind = TimeoutKind.TOOL if record.command_timeout_sec else TimeoutKind.PROCESS

An execution whose own deadline had already passed, and whose child command was
additionally over its command limit, is therefore reported as a *tool* timeout.
A reader cannot tell an under-specified caller from a hung child process, and
cannot tell a total-runtime expiry from an inactivity expiry, because the record
does not say which budget was measured or which layer the expiry belongs to.

So a timeout here is not a boolean. It is a structured attribution:

* the **kind** — which clock, including the execution's own deadline clock,
  which no caller could previously name;
* the **budget** — the seconds the clock was measured against, and which
  declared field that number came from;
* the **elapsed** time, so an expiry is checkable rather than asserted;
* the **layer** the expiry is charged to, which is what a reader needs in order
  to decide whether the execution is at fault or the work under it is.

Nothing is removed. The existing timeout path still fires, still persists, and
still produces ``timeout_type``/``timeout_seconds``. This module adds the
structure those two free-form fields could not carry, and is the one place a
timeout attribution is constructed.

The execution-level deadline is *declarable and attributable* here. Arming a new
kill loop for it is a behaviour change and deliberately not done: enforcement
stays where it already is.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

__all__ = [
    "TIMEOUT_LAYERS",
    "ExecutionPolicy",
    "ExecutionTimeoutAttribution",
    "ExecutionTimeoutPolicy",
    "TimeoutBudgetSource",
    "TimeoutKind",
]


class TimeoutKind(StrEnum):
    """How a timeout was produced. Kept separate from the failure class so the
    receipt can say *which clock* expired, not merely that something did."""

    SUBMIT = "SUBMIT_TIMEOUT"
    PROCESS = "PROCESS_TIMEOUT"
    TOOL = "TOOL_TIMEOUT"
    #: The execution's own total-runtime deadline. Distinct from every child
    #: clock: an execution can outlive its deadline while every command and
    #: tool inside it is individually well within budget, and the previous
    #: table had no way to say so.
    EXECUTION = "EXECUTION_TIMEOUT"


class TimeoutBudgetSource(StrEnum):
    """Which declared budget a timeout was measured against.

    ``UNBOUNDED`` is the honest answer for a clock with no declared budget. It
    matters that this value exists: without it, a timeout on an unbounded clock
    is indistinguishable from a typo in the budget that produced it.
    """

    EXECUTION_DEADLINE = "EXECUTION_DEADLINE"
    COMMAND_LIMIT = "COMMAND_LIMIT"
    EFFECTIVE_TIMEOUT_MS = "EFFECTIVE_TIMEOUT_MS"
    UNBOUNDED = "UNBOUNDED"


#: The layer an expiry is charged to. Separate from :class:`TimeoutKind` because
#: the kind names a clock and the layer names who is answerable for it.
TIMEOUT_LAYERS: dict[str, str] = {
    TimeoutKind.EXECUTION: "execution",
    TimeoutKind.TOOL: "tool",
    TimeoutKind.PROCESS: "process",
    TimeoutKind.SUBMIT: "submit",
}


@dataclass(frozen=True)
class ExecutionPolicy:
    """Canonical execution-level clocks; legacy timeout fields are projections."""

    idle_timeout_ms: int
    max_runtime_ms: int
    heartbeat_interval_ms: int = 60_000
    checkpoint_interval_ms: int = 30_000
    provider_request_timeout_ms: int = 120_000

    def __post_init__(self) -> None:
        for name in (
            "idle_timeout_ms", "max_runtime_ms", "heartbeat_interval_ms",
            "checkpoint_interval_ms", "provider_request_timeout_ms",
        ):
            if int(getattr(self, name)) <= 0:
                raise ValueError(f"{name} must be positive")
        if not self.provider_request_timeout_ms < self.idle_timeout_ms < self.max_runtime_ms:
            raise ValueError(
                "provider_request_timeout_ms < idle_timeout_ms < max_runtime_ms is required"
            )

    @classmethod
    def from_legacy(cls, requested_timeout_s: float, *, separated_cli: bool = True) -> ExecutionPolicy:
        requested_ms = max(1_000, int(float(requested_timeout_s) * 1000))
        if separated_cli:
            max_ms = max(requested_ms, 1_800_000)
            idle_ms = min(requested_ms, 300_000)
            if idle_ms >= max_ms:
                idle_ms = max_ms - 1_000
            provider_ms = min(120_000, max(1_000, idle_ms // 2))
        else:
            max_ms = max(requested_ms, 2_000)
            idle_ms = min(max_ms - 1_000, requested_ms)
            provider_ms = min(60_000, max(1_000, idle_ms // 2))
        return cls(
            idle_timeout_ms=idle_ms,
            max_runtime_ms=max_ms,
            provider_request_timeout_ms=provider_ms,
        )

    def to_dict(self) -> dict[str, int]:
        return {
            "idle_timeout_ms": self.idle_timeout_ms,
            "max_runtime_ms": self.max_runtime_ms,
            "heartbeat_interval_ms": self.heartbeat_interval_ms,
            "checkpoint_interval_ms": self.checkpoint_interval_ms,
            "provider_request_timeout_ms": self.provider_request_timeout_ms,
        }


@dataclass(frozen=True)
class ExecutionTimeoutPolicy:
    """The clocks an execution declared, in one addressable place.

    Frozen so a policy cannot be edited into agreeing with a timeout that has
    already happened. Bounds are positive seconds or ``None`` for unbounded; a
    zero or negative budget is refused at construction rather than being allowed
    to expire a record the instant it starts.
    """

    execution_deadline_s: float | None = None
    command_limit_s: float | None = None
    effective_timeout_ms: int | None = None

    def __post_init__(self) -> None:
        for name in ("execution_deadline_s", "command_limit_s"):
            value = getattr(self, name)
            if value is not None and float(value) <= 0.0:
                raise ValueError(
                    f"{name} must be positive seconds or None, got {value!r}. "
                    "A non-positive budget expires on arrival; declare it unbounded instead."
                )
        if self.effective_timeout_ms is not None and int(self.effective_timeout_ms) <= 0:
            raise ValueError(
                f"effective_timeout_ms must be positive or None, got {self.effective_timeout_ms!r}"
            )

    @classmethod
    def from_record(cls, record: Any) -> ExecutionTimeoutPolicy:
        """Project the policy out of an ExecutionRecord, or any object with the
        same three fields. Reading a record cannot widen it."""

        return cls(
            execution_deadline_s=getattr(record, "execution_timeout_sec", None),
            command_limit_s=getattr(record, "command_timeout_sec", None),
            effective_timeout_ms=getattr(record, "effective_timeout_ms", None),
        )

    def resolve(self, kind: str | TimeoutKind) -> float | None:
        """The budget a given clock runs against, in seconds."""

        source = self.budget_source_for(kind)
        if source is TimeoutBudgetSource.EFFECTIVE_TIMEOUT_MS:
            return float(self.effective_timeout_ms or 0) / 1000.0
        if source is TimeoutBudgetSource.COMMAND_LIMIT:
            return self.command_limit_s
        if source is TimeoutBudgetSource.EXECUTION_DEADLINE:
            return self.execution_deadline_s
        return None

    def budget_source_for(self, kind: str | TimeoutKind) -> TimeoutBudgetSource:
        """Which declared field supplies this clock's budget.

        The mapping is by clock, not by convenience. A tool timeout is measured
        against the command limit even when an execution deadline also exists,
        because the command limit is the one that was actually armed for the
        command.
        """

        if kind == TimeoutKind.TOOL:
            return (
                TimeoutBudgetSource.COMMAND_LIMIT
                if self.command_limit_s is not None
                else TimeoutBudgetSource.EXECUTION_DEADLINE
            )
        if kind == TimeoutKind.SUBMIT:
            return TimeoutBudgetSource.EFFECTIVE_TIMEOUT_MS
        return TimeoutBudgetSource.EXECUTION_DEADLINE

    def expired(self, kind: str | TimeoutKind, elapsed_s: float) -> bool:
        """Whether a clock with this budget has been exceeded.

        An unbounded clock never expires. Without that, an execution with no
        declared deadline would be reported as immediately timed out.
        """

        budget = self.resolve(kind)
        if budget is None:
            return False
        return float(elapsed_s) >= float(budget)

    def attribute(
        self,
        kind: str | TimeoutKind,
        *,
        elapsed_s: float | None = None,
        at: float | None = None,
        seconds: float | None = None,
    ) -> ExecutionTimeoutAttribution:
        """Build the structured attribution for a clock that expired.

        ``seconds`` overrides the resolved budget for callers that expired
        against a budget not declared on the record (a caller-supplied clock).
        The override is still recorded, and ``budget_seconds`` says the caller
        supplied it, so a hand-passed number is not mistaken for a declared one.
        """

        if str(kind) not in {str(k) for k in TimeoutKind}:
            raise ValueError(
                f"unknown timeout kind: {kind!r}. Known clocks: "
                f"{', '.join(sorted(str(k) for k in TimeoutKind))}"
            )
        kind_value = TimeoutKind(str(kind))
        source = self.budget_source_for(kind_value)
        resolved = self.resolve(kind_value)
        return ExecutionTimeoutAttribution(
            kind=str(kind_value),
            layer=TIMEOUT_LAYERS[kind_value],
            budget_source=(
                TimeoutBudgetSource.EXECUTION_DEADLINE if seconds is not None else source
            ),
            budget_seconds=float(seconds) if seconds is not None else resolved,
            budget_is_declared=seconds is None and resolved is not None,
            elapsed_seconds=None if elapsed_s is None else round(float(elapsed_s), 6),
            attributed_at=float(at) if at is not None else time.time(),
        )


@dataclass(frozen=True)
class ExecutionTimeoutAttribution:
    """One timeout, resolved to the clock, budget and layer it belongs to.

    ``budget_is_declared`` is the field that makes a receipt trustworthy: it
    separates "this execution declared a 900s deadline and used 903s" from
    "this execution declared nothing and something reported 900s anyway".
    """

    kind: str
    layer: str
    budget_source: str
    budget_seconds: float | None
    budget_is_declared: bool
    elapsed_seconds: float | None
    attributed_at: float

    @property
    def is_execution_deadline(self) -> bool:
        return str(self.kind) == str(TimeoutKind.EXECUTION)

    def to_dict(self) -> dict[str, Any]:
        """Receipt projection: an explicit allowlist, no attribute dump."""

        return {
            "kind": str(self.kind),
            "layer": str(self.layer),
            "budget_source": str(self.budget_source),
            "budget_seconds": self.budget_seconds,
            "budget_is_declared": self.budget_is_declared,
            "elapsed_seconds": self.elapsed_seconds,
            "attributed_at": self.attributed_at,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> ExecutionTimeoutAttribution:
        return cls(
            kind=str(payload["kind"]),
            layer=str(payload["layer"]),
            budget_source=str(payload["budget_source"]),
            budget_seconds=(
                float(payload["budget_seconds"])
                if payload.get("budget_seconds") is not None
                else None
            ),
            budget_is_declared=bool(payload.get("budget_is_declared", False)),
            elapsed_seconds=(
                float(payload["elapsed_seconds"])
                if payload.get("elapsed_seconds") is not None
                else None
            ),
            attributed_at=float(payload["attributed_at"]),
        )
