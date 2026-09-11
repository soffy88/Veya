"""Crash-safe control plane for long-running GoalRun work.

This module is deliberately a controller, not another execution runtime.  The
caller owns the existing GoalRun, PersistentComputer and Verification OS
objects and supplies their identifiers/results.  The controller only records
progress, applies finite guards, and persists a resumable projection.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


class HarnessError(RuntimeError):
    """Base error for deterministic harness decisions."""


class DuplicateFailedAction(HarnessError):
    """The same failed action was proposed again."""


class BudgetExhausted(HarnessError):
    """No further work may be accepted by this run."""


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _digest(value: Any) -> str:
    return hashlib.sha256(_json(value).encode()).hexdigest()


@dataclass
class LongRunBudget:
    max_wall_s: float = 3600.0
    max_tool_calls: int = 1000
    max_replans: int = 20
    max_retrievals: int = 200
    max_context_tokens: int = 1_000_000


@dataclass
class ProgressObservation:
    """Facts produced by one round; no fact is inferred from model prose."""

    artifacts: list[str] = field(default_factory=list)
    state_hash: str | None = None
    tool_result_hash: str | None = None
    verification_progress: str | None = None
    plan_step: str | None = None

    def fingerprint(self) -> str:
        return _digest(asdict(self))

    def progressed_from(self, previous: ProgressObservation | None) -> bool:
        if previous is None:
            return True
        return bool(
            set(self.artifacts) - set(previous.artifacts)
            or self.state_hash != previous.state_hash
            or self.tool_result_hash != previous.tool_result_hash
            or self.verification_progress != previous.verification_progress
            or self.plan_step != previous.plan_step
        )


@dataclass
class LongRunState:
    goal_run_id: str
    computer_id: str
    status: str = "running"
    plan: list[str] = field(default_factory=list)
    current_step: str | None = None
    observations: list[dict[str, Any]] = field(default_factory=list)
    failure_evidence: list[dict[str, Any]] = field(default_factory=list)
    verification_state: dict[str, Any] = field(default_factory=dict)
    provider_state_refs: list[str] = field(default_factory=list)
    tool_calls: int = 0
    replans: int = 0
    retrievals: int = 0
    context_tokens: int = 0
    no_progress_rounds: int = 0
    failed_action_keys: list[str] = field(default_factory=list)
    # Wall clock is persisted so a supervisor restart cannot reset the time budget.
    started_at: float = field(default_factory=time.time)
    last_checkpoint_at: float = 0.0
    suspend_reason: str | None = None


class LongRunCheckpointStore:
    """Atomic, task-scoped checkpoint storage."""

    def __init__(self, run_root: str | Path):
        self.path = Path(run_root).expanduser().resolve() / "checkpoints" / "long-running.json"

    def write(self, state: LongRunState) -> Path:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(_json(asdict(state)), encoding="utf-8")
        temporary.replace(self.path)
        return self.path

    def read(self) -> LongRunState | None:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            return LongRunState(**value)
        except (OSError, ValueError, TypeError):
            return None


class LongRunningHarness:
    """Finite progress/recovery policy around the existing execution runtime."""

    def __init__(
        self,
        state: LongRunState,
        budget: LongRunBudget | None = None,
        *,
        checkpoint_store: LongRunCheckpointStore | None = None,
        stall_threshold: int = 3,
    ):
        if not state.goal_run_id or not state.computer_id:
            raise ValueError("goal_run_id and computer_id are required")
        self.state = state
        self.budget = budget or LongRunBudget()
        self.checkpoint_store = checkpoint_store
        self.stall_threshold = max(1, stall_threshold)
        self._last_progress = (
            ProgressObservation(**state.observations[-1]) if state.observations else None
        )
        self._failed_actions: dict[str, int] = {key: 1 for key in state.failed_action_keys}

    @classmethod
    def resume(
        cls,
        checkpoint_store: LongRunCheckpointStore,
        budget: LongRunBudget | None = None,
        **kwargs: Any,
    ) -> LongRunningHarness:
        state = checkpoint_store.read()
        if state is None:
            raise HarnessError("checkpoint not found or invalid")
        return cls(state, budget, checkpoint_store=checkpoint_store, **kwargs)

    def checkpoint(self, *, reason: str = "periodic") -> Path | None:
        self.state.last_checkpoint_at = time.time()
        if self.checkpoint_store is None:
            return None
        return self.checkpoint_store.write(self.state)

    def _ensure_capacity(
        self, *, tool: bool = False, retrieval: bool = False, context_tokens: int = 0
    ) -> None:
        elapsed = time.time() - self.state.started_at
        if elapsed >= self.budget.max_wall_s:
            self.suspend("time_budget")
        if tool and self.state.tool_calls >= self.budget.max_tool_calls:
            self.suspend("tool_call_budget")
        if retrieval and self.state.retrievals >= self.budget.max_retrievals:
            self.suspend("retrieval_budget")
        if self.state.context_tokens + context_tokens > self.budget.max_context_tokens:
            self.suspend("context_budget")
        if self.state.status in {"suspended", "blocked", "completed"}:
            raise BudgetExhausted(self.state.suspend_reason or self.state.status)

    def record_progress(self, observation: ProgressObservation, *, context_tokens: int = 0) -> bool:
        self._ensure_capacity(context_tokens=context_tokens)
        progressed = observation.progressed_from(self._last_progress)
        self._last_progress = observation
        self.state.observations.append(asdict(observation))
        self.state.context_tokens += max(0, context_tokens)
        self.state.current_step = observation.plan_step or self.state.current_step
        self.state.no_progress_rounds = 0 if progressed else self.state.no_progress_rounds + 1
        # In-memory counters must not be the only source of truth across a
        # supervisor restart.
        self.checkpoint(reason="progress")
        if self.state.no_progress_rounds >= self.stall_threshold:
            self.replan("no_progress_stall")
        return progressed

    def action_key(
        self, tool: str, args: Mapping[str, Any], environment: Mapping[str, Any] | None = None
    ) -> str:
        return _digest({"tool": tool, "args": dict(args), "environment": dict(environment or {})})

    def before_action(
        self, tool: str, args: Mapping[str, Any], environment: Mapping[str, Any] | None = None
    ) -> None:
        self._ensure_capacity(tool=True)
        key = self.action_key(tool, args, environment)
        if self._failed_actions.get(key, 0):
            self.replan("duplicate_failed_action")
            raise DuplicateFailedAction(key)
        self.state.tool_calls += 1
        self.checkpoint(reason="action_admitted")

    def record_action_failure(
        self,
        tool: str,
        args: Mapping[str, Any],
        error: str,
        environment: Mapping[str, Any] | None = None,
    ) -> None:
        key = self.action_key(tool, args, environment)
        self._failed_actions[key] = self._failed_actions.get(key, 0) + 1
        if key not in self.state.failed_action_keys:
            self.state.failed_action_keys.append(key)
        self.state.failure_evidence.append({"action_key": key, "tool": tool, "error": error})
        self.checkpoint(reason="action_failure")

    def replan(self, reason: str) -> None:
        if self.state.replans >= self.budget.max_replans:
            self.suspend("replan_budget")
            raise BudgetExhausted("replan_budget")
        self.state.replans += 1
        self.state.status = "recovering"
        self.state.suspend_reason = reason
        self.state.no_progress_rounds = 0
        self.checkpoint(reason=reason)

    async def provider_call(
        self,
        request: Callable[[str], Awaitable[Any]],
        providers: list[str],
        *,
        state_refs: list[str] | None = None,
    ) -> Any:
        """Try providers in router order while retaining this same GoalRun."""
        last_error: Exception | None = None
        for provider in providers:
            try:
                result = await request(provider)
                if result is None or result == "":
                    raise HarnessError("empty provider response")
                self.state.provider_state_refs.extend(state_refs or [provider])
                self.state.status = "running"
                self.checkpoint(reason="provider_recovered")
                return result
            except Exception as exc:
                last_error = exc
                self.state.failure_evidence.append({"provider": provider, "error": str(exc)})
        self.suspend("provider_exhausted")
        raise HarnessError(f"all providers failed: {last_error}")

    def record_retrieval(self, observation: ProgressObservation) -> bool:
        self._ensure_capacity(retrieval=True)
        self.state.retrievals += 1
        return self.record_progress(observation)

    def apply_verification(self, outcome: str, *, evidence: Mapping[str, Any] | None = None) -> str:
        normalized = str(outcome).upper()
        self.state.verification_state = {"outcome": normalized, "evidence": dict(evidence or {})}
        if normalized == "PASS":
            self.state.status = "completed"
            self.state.suspend_reason = None
            self.checkpoint(reason="verification_pass")
            return self.state.status
        if normalized in {"FAIL", "BLOCKED"}:
            self.state.failure_evidence.append(
                {"verification": normalized, "evidence": dict(evidence or {})}
            )
            self.replan(f"verification_{normalized.lower()}")
            return self.state.status
        raise ValueError(f"unknown verification outcome: {outcome}")

    def suspend(self, reason: str) -> None:
        self.state.status = "suspended"
        self.state.suspend_reason = reason
        self.checkpoint(reason=reason)
