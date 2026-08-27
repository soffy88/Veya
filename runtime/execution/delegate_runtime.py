"""Execution boundary for one isolated delegate."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any

from .adapters import delegate_result_from_mapping
from .models import DelegateRequest, DelegateResult
from .spawn_guard import SpawnGuard, SpawnRejected


class DelegateRuntime:
    """Run a delegate under one shared ``SpawnGuard``.

    The callback owns semantic work and returns a mapping or DelegateResult;
    this class owns admission, timeout, cancellation-safe cleanup and lifecycle
    events only.
    """

    def __init__(
        self, guard: SpawnGuard, *, on_event: Callable[[dict[str, Any]], Any] | None = None
    ):
        self.guard = guard
        self.on_event = on_event

    async def _emit(self, event: dict[str, Any]) -> None:
        if self.on_event is None:
            return
        value = self.on_event(event)
        if asyncio.iscoroutine(value):
            await value

    async def run(
        self,
        request: DelegateRequest,
        operation: Callable[[asyncio.Event], Awaitable[DelegateResult | dict[str, Any]]],
    ) -> DelegateResult:
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
            return result

        await self._emit({"type": "delegate.started", "delegate_id": request.delegate_id})
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
        return result
