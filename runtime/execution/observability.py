"""Metrics, health and incident-recovery projections for durable execution.

The module is deliberately data-plane neutral: it observes existing GoalRun,
worker and side-effect facts and invokes caller-owned recovery callbacks.  It
does not execute work, schedule work, or decide acceptance.
"""

from __future__ import annotations

import inspect
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ExecutionMetrics:
    """Process-local projection; durable event logs remain the source of truth."""

    goalrun_latencies_s: list[float] = field(default_factory=list)
    goalrun_total: int = 0
    goalrun_success: int = 0
    recovery_total: int = 0
    recovery_success: int = 0
    duplicate_side_effects: int = 0
    side_effect_attempts: int = 0
    false_success: int = 0
    queue_depth: int = 0
    active_bots: int = 0
    active_runs: int = 0
    provider_failures: int = 0
    tool_failures: int = 0
    restart_count: int = 0
    replan_count: int = 0
    verifier_count: int = 0
    budget_exhaustion_count: int = 0

    def record_goalrun(self, started_at: float, ended_at: float, status: str) -> None:
        self.goalrun_total += 1
        self.goalrun_latencies_s.append(max(0.0, ended_at - started_at))
        if status == "success":
            self.goalrun_success += 1

    def record_recovery(self, success: bool) -> None:
        self.recovery_total += 1
        self.recovery_success += int(success)

    def record_failure(self, *, provider: bool = False, tool: bool = False) -> None:
        self.provider_failures += int(provider)
        self.tool_failures += int(tool)

    def record_side_effect(self, *, duplicate: bool = False) -> None:
        self.side_effect_attempts += 1
        self.duplicate_side_effects += int(duplicate)

    def record_control_event(self, event: str) -> None:
        if event == "restart":
            self.restart_count += 1
        elif event == "replan":
            self.replan_count += 1
        elif event == "verifier":
            self.verifier_count += 1
        elif event == "budget_exhausted":
            self.budget_exhaustion_count += 1

    def snapshot(self) -> dict[str, Any]:
        return {
            "goalrun_latency_seconds": list(self.goalrun_latencies_s),
            "queue_depth": self.queue_depth,
            "active_bots": self.active_bots,
            "active_runs": self.active_runs,
            "provider_failures": self.provider_failures,
            "tool_failures": self.tool_failures,
            "restart_count": self.restart_count,
            "replan_count": self.replan_count,
            "verifier_count": self.verifier_count,
            "budget_exhaustion_count": self.budget_exhaustion_count,
            "duplicate_side_effect_count": self.duplicate_side_effects,
        }


class SLOCalculator:
    """Calculate release SLOs from the metrics projection."""

    def __init__(self, metrics: ExecutionMetrics):
        self.metrics = metrics

    def calculate(self) -> dict[str, float | None]:
        latencies = sorted(self.metrics.goalrun_latencies_s)
        if not latencies:
            p95 = None
        else:
            index = max(0, min(len(latencies) - 1, int(len(latencies) * 0.95 + 0.999) - 1))
            p95 = latencies[index]
        return {
            "GOALRUN_SUCCESS_RATE": self.metrics.goalrun_success / self.metrics.goalrun_total
            if self.metrics.goalrun_total
            else None,
            "P95_GOALRUN_LATENCY": p95,
            "RECOVERY_SUCCESS_RATE": self.metrics.recovery_success / self.metrics.recovery_total
            if self.metrics.recovery_total
            else None,
            "DUPLICATE_SIDE_EFFECT_RATE": self.metrics.duplicate_side_effects
            / self.metrics.side_effect_attempts
            if self.metrics.side_effect_attempts
            else 0.0,
            "FALSE_SUCCESS_RATE": self.metrics.false_success / self.metrics.goalrun_total
            if self.metrics.goalrun_total
            else 0.0,
        }


class HealthReadiness:
    """Fail-closed health probes for the existing durable runtime."""

    @staticmethod
    def liveness() -> dict[str, Any]:
        return {"ok": True, "status": "live"}

    @staticmethod
    async def readiness(
        *,
        durable_backend: Callable[[], Any],
        provider: Callable[[], Any],
        supervisor: Callable[[], Any],
    ) -> dict[str, Any]:
        checks: dict[str, Any] = {}
        for name, probe in (
            ("durable_backend", durable_backend),
            ("provider", provider),
            ("supervisor", supervisor),
        ):
            try:
                result = probe()
                if inspect.isawaitable(result):
                    result = await result
                checks[name] = result if isinstance(result, dict) else {"ok": bool(result)}
            except Exception as exc:
                checks[name] = {"ok": False, "error": type(exc).__name__}
        ready = all(
            bool(value.get("ok")) and not value.get("degraded", False) for value in checks.values()
        )
        return {"ok": ready, "status": "ready" if ready else "not_ready", "checks": checks}


class TraceCorrelator:
    """Join audit facts without granting any component authority."""

    def __init__(self):
        self.events: list[dict[str, Any]] = []

    def record(
        self,
        *,
        trace_id: str,
        bot_id: str,
        goal_run_id: str,
        action_id: str | None = None,
        delegate_id: str | None = None,
        verifier_id: str | None = None,
        event: str,
        **payload: Any,
    ) -> dict[str, Any]:
        item = {
            "trace_id": trace_id,
            "bot_id": bot_id,
            "goal_run_id": goal_run_id,
            "action_id": action_id,
            "delegate_id": delegate_id,
            "verifier_id": verifier_id,
            "event": event,
            **payload,
        }
        self.events.append(item)
        return item

    def trace(self, trace_id: str) -> list[dict[str, Any]]:
        return [item for item in self.events if item["trace_id"] == trace_id]

    def is_complete(self, trace_id: str) -> bool:
        events = self.trace(trace_id)
        return (
            bool(events)
            and all(events[-1].get(field) for field in ("bot_id", "goal_run_id"))
            and any(item.get("verifier_id") for item in events)
        )


RecoveryCallback = Callable[[], Any | Awaitable[Any]]


class IncidentRecovery:
    """One fail-closed recovery contract for provider/tool/worker incidents."""

    def __init__(
        self, metrics: ExecutionMetrics, *, on_event: Callable[[dict[str, Any]], Any] | None = None
    ):
        self.metrics = metrics
        self.on_event = on_event
        self.incidents: Counter[str] = Counter()

    async def run(
        self, incident: str, operation: RecoveryCallback, recover: RecoveryCallback
    ) -> Any:
        try:
            result = operation()
            if inspect.isawaitable(result):
                result = await result
            self.metrics.record_recovery(True)
            return result
        except Exception as original:
            self.incidents[incident] += 1
            self._emit(
                {"event": "incident", "incident": incident, "error": type(original).__name__}
            )
            try:
                result = recover()
                if inspect.isawaitable(result):
                    result = await result
            except Exception as recovery_error:
                self.metrics.record_recovery(False)
                self._emit({"event": "recovery.failed", "incident": incident})
                raise RuntimeError(
                    f"{incident} recovery failed; execution remains blocked"
                ) from recovery_error
            self.metrics.record_recovery(True)
            self._emit({"event": "recovery.completed", "incident": incident})
            return result

    def _emit(self, event: dict[str, Any]) -> None:
        if self.on_event is not None:
            self.on_event(event)


__all__ = [
    "ExecutionMetrics",
    "HealthReadiness",
    "IncidentRecovery",
    "SLOCalculator",
    "TraceCorrelator",
]
