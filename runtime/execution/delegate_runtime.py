"""Execution boundary for one isolated delegate."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any

from runtime.bot_scope import DEFAULT_BOT_ID, require_same_bot

from .adapters import delegate_result_from_mapping
from .models import (
    PARALLEL_EXECUTION_AUTHORITY_COUNT,
    SECOND_EXECUTION_AUTHORITY,
    SUBAGENT_ACCEPTANCE_AUTHORITY,
    SUBAGENT_DIRECT_PHYSICAL_EXECUTION,
    SUBAGENT_EXECUTION_AUTHORITY,
    SUBAGENT_GOALRUN_CREATION,
    DelegateRequest,
    DelegateResult,
    assert_parallel_execution_authority_single,
    assert_second_execution_authority_zero,
    assert_subagent_acceptance_authority_zero,
    assert_subagent_direct_physical_execution_zero,
    assert_subagent_execution_authority_zero,
    assert_subagent_goalrun_creation_zero,
)
from .spawn_guard import SpawnGuard, SpawnRejected


class DelegateRuntime:
    """Run a delegate under one shared ``SpawnGuard``.

    The callback owns semantic work and returns a mapping or DelegateResult;
    this class owns admission, timeout, cancellation-safe cleanup and lifecycle
    events only.

    P2-A: every delegate runs inside the SAME GoalRun that admitted it.
    Physical work always funnels through ``CanonicalActionRequest → GoalRun →
    ActionGateway`` downstream; this class never executes physical actions and
    never creates a GoalRun (it holds no store reference — persistence flows
    out through ``on_delegate_state`` into the existing GoalRunState).
    """

    def __init__(
        self,
        guard: SpawnGuard,
        *,
        on_event: Callable[[dict[str, Any]], Any] | None = None,
        goal_run_id: str | None = None,
        on_delegate_state: Callable[[dict[str, Any]], Any] | None = None,
        bot_id: str | None = None,
    ):
        self.guard = guard
        self.on_event = on_event
        self.goal_run_id = goal_run_id
        self.on_delegate_state = on_delegate_state
        # P3-A: when set, only delegates owned by this bot may run here.
        self.bot_id = bot_id if bot_id is not None else DEFAULT_BOT_ID
        self._completed: dict[str, DelegateResult] = {}
        self._attempts: dict[str, int] = {}

    async def _emit(self, event: dict[str, Any]) -> None:
        if self.on_event is None:
            return
        value = self.on_event(event)
        if asyncio.iscoroutine(value):
            await value

    async def _record_state(self, projection: dict[str, Any]) -> None:
        if self.on_delegate_state is None:
            return
        value = self.on_delegate_state(projection)
        if asyncio.iscoroutine(value):
            await value

    async def run(
        self,
        request: DelegateRequest,
        operation: Callable[[asyncio.Event], Awaitable[DelegateResult | dict[str, Any]]],
        *,
        goal_run_id: str | None = None,
        physical_action_count: int = 0,
    ) -> DelegateResult:
        # P2-A §6/§9: single execution authority, zero subagent authority.
        assert_parallel_execution_authority_single(PARALLEL_EXECUTION_AUTHORITY_COUNT)
        assert_second_execution_authority_zero(SECOND_EXECUTION_AUTHORITY)
        assert_subagent_execution_authority_zero()
        assert_subagent_acceptance_authority_zero()
        assert_subagent_goalrun_creation_zero(SUBAGENT_GOALRUN_CREATION)
        assert_subagent_direct_physical_execution_zero(physical_action_count)
        if SUBAGENT_EXECUTION_AUTHORITY != 0 or SUBAGENT_ACCEPTANCE_AUTHORITY != 0:
            raise AssertionError("subagent authority must stay 0")
        if SUBAGENT_DIRECT_PHYSICAL_EXECUTION != 0:
            raise AssertionError("subagent direct physical execution must stay 0")
        # P2-A: SAME GoalRun — the delegate never leaves its parent run.
        effective_goal_run_id = goal_run_id if goal_run_id is not None else self.goal_run_id
        if effective_goal_run_id is not None and request.parent_trace_id != effective_goal_run_id:
            raise ValueError(
                "delegate belongs to a different GoalRun: "
                f"{request.parent_trace_id!r} != {effective_goal_run_id!r}"
            )
        # P3-A: SAME Bot — a delegate owned by another bot never runs here.
        require_same_bot(request.bot_id, self.bot_id, f"delegate:{request.delegate_id}")
        # Duplicate execution guard: a completed delegate is never re-run.
        stored = self._completed.get(request.delegate_id)
        if stored is not None and stored.status == "complete":
            return stored
        started_at = time.monotonic()
        await self._emit({"type": "delegate.queued", "delegate_id": request.delegate_id})
        try:
            await self.guard.pre_check(
                depth=request.depth,
                estimated_tokens=request.estimated_tokens,
                estimated_cost_usd=request.budget_usd or 0.0,
            )
        except SpawnRejected as exc:
            result = DelegateResult(
                delegate_id=request.delegate_id,
                status="failed",
                stop_reason="budget_exhausted" if "budget" in str(exc) else "exception",
                summary="",
                child_trace_id=request.parent_trace_id,
                error_class="spawn_rejected",
                error_message=str(exc),
            )
            await self._emit(
                {"type": "delegate.failed", "delegate_id": request.delegate_id, "error": str(exc)}
            )
            await self._record_state(
                {
                    "delegate_id": request.delegate_id,
                    "parent_goal_run_id": effective_goal_run_id or request.parent_trace_id,
                    "bot_id": self.bot_id,
                    "status": result.status,
                    "request_ref": request.delegate_id,
                    "result_ref": request.delegate_id,
                    "evidence_refs": [],
                    "attempt": self._attempts.get(request.delegate_id, 0),
                    "replan": False,
                    "stopped_at": time.time(),
                }
            )
            return result

        await self._emit({"type": "delegate.started", "delegate_id": request.delegate_id})
        self._attempts[request.delegate_id] = self._attempts.get(request.delegate_id, 0) + 1
        await self._record_state(
            {
                "delegate_id": request.delegate_id,
                "parent_goal_run_id": effective_goal_run_id or request.parent_trace_id,
                "bot_id": self.bot_id,
                "status": "running",
                "request_ref": request.delegate_id,
                "result_ref": None,
                "evidence_refs": [],
                "attempt": self._attempts[request.delegate_id],
                "replan": self._attempts[request.delegate_id] > 1,
                "stopped_at": None,
            }
        )
        try:
            raw = await self.guard.run(
                request.delegate_id,
                operation,
                depth=request.depth,
                estimated_tokens=request.estimated_tokens,
                estimated_cost_usd=request.budget_usd or 0.0,
                timeout_s=request.timeout_s,
            )
        except TimeoutError:
            result = DelegateResult(
                delegate_id=request.delegate_id,
                status="partial",
                stop_reason="wall_deadline",
                summary="delegate timeout; partial work was preserved where available",
                unfinished_work=[request.objective],
                child_trace_id=request.parent_trace_id,
                duration_ms=round((time.monotonic() - started_at) * 1000),
                error_class="timeout",
                error_message="delegate timeout",
            )
        except asyncio.CancelledError:
            result = DelegateResult(
                delegate_id=request.delegate_id,
                status="cancelled",
                stop_reason="cancelled",
                summary="delegate cancelled",
                unfinished_work=[request.objective],
                child_trace_id=request.parent_trace_id,
                duration_ms=round((time.monotonic() - started_at) * 1000),
            )
        except Exception as exc:
            result = DelegateResult(
                delegate_id=request.delegate_id,
                status="failed",
                stop_reason="exception",
                summary="",
                unfinished_work=[request.objective],
                child_trace_id=request.parent_trace_id,
                duration_ms=round((time.monotonic() - started_at) * 1000),
                error_class=type(exc).__name__,
                error_message=str(exc),
            )
        else:
            if isinstance(raw, DelegateResult):
                result = raw
            else:
                result = delegate_result_from_mapping(request, raw)
            result.duration_ms = result.duration_ms or round((time.monotonic() - started_at) * 1000)
        # Keep the lifecycle vocabulary identical to the public event model:
        # ``complete`` is the result status, while ``completed`` is the event.
        event_status = {"complete": "completed"}.get(result.status, result.status)
        await self._emit(
            {
                "type": f"delegate.{event_status}",
                "delegate_id": request.delegate_id,
                "stop_reason": result.stop_reason,
            }
        )
        if result.status == "complete":
            self._completed[request.delegate_id] = result
        await self._record_state(
            {
                "delegate_id": request.delegate_id,
                "parent_goal_run_id": effective_goal_run_id or request.parent_trace_id,
                "bot_id": self.bot_id,
                "status": result.status,
                "request_ref": request.delegate_id,
                "result_ref": request.delegate_id,
                "evidence_refs": [
                    item.sha256 for item in (result.evidence or []) if getattr(item, "sha256", None)
                ],
                "attempt": self._attempts.get(request.delegate_id, 1),
                "replan": False,
                "stopped_at": time.time(),
            }
        )
        return result
