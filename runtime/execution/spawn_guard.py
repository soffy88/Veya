"""Concurrency, depth and budget guard for all delegated execution."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

from .models import SpawnBudget, SpawnReservation


class SpawnRejected(RuntimeError):
    """A child cannot be admitted under the current deterministic limits."""


class SpawnGuard:
    """RAII admission control shared by AgentLoop, Hicode and GoalRun leaves.

    Waiting for a full concurrency limit is intentional: queued work is not
    rejected.  Reservations are released on cancellation before semaphore
    acquisition and on every exception after acquisition.
    """

    def __init__(
        self,
        budget: SpawnBudget | None = None,
        *,
        clock: Callable[[], float] | None = None,
        on_event: Callable[[dict[str, Any]], Any] | None = None,
    ):
        self.budget = budget or SpawnBudget()
        if self.budget.max_depth < 0:
            raise ValueError("max_depth must be non-negative")
        if self.budget.max_parallel < 1:
            raise ValueError("max_parallel must be at least 1")
        if self.budget.max_tokens < 0:
            raise ValueError("max_tokens must be non-negative")
        self._clock = clock or time.monotonic
        self._on_event = on_event
        self._started_at = self._clock()
        self._semaphore = asyncio.Semaphore(self.budget.max_parallel)
        self._reserved_tokens = 0
        self._used_tokens = 0
        self._reserved_cost = 0.0
        self._used_cost = 0.0
        self._lock = asyncio.Lock()
        self._leases: dict[str, SpawnReservation] = {}
        self._tasks: dict[str, asyncio.Task[Any]] = {}
        self._cancel_events: dict[str, asyncio.Event] = {}

    async def _emit(self, event: dict[str, Any]) -> None:
        if self._on_event is None:
            return
        try:
            value = self._on_event(event)
            if asyncio.iscoroutine(value):
                await value
        except Exception:
            # Telemetry is fail-open; admission and cleanup remain authoritative.
            return

    @property
    def active_count(self) -> int:
        return sum(1 for lease in self._leases.values() if lease.acquired and not lease.released)

    @property
    def queued_count(self) -> int:
        return sum(
            1 for lease in self._leases.values() if not lease.acquired and not lease.released
        )

    def remaining_wall_s(self) -> float:
        return max(0.0, float(self.budget.root_wall_time_s) - (self._clock() - self._started_at))

    async def pre_check(
        self,
        *,
        depth: int,
        estimated_tokens: int = 0,
        estimated_cost_usd: float = 0.0,
    ) -> None:
        if depth >= self.budget.max_depth:
            raise SpawnRejected(
                f"delegation depth limit reached: depth={depth}, max={self.budget.max_depth}"
            )
        if depth < 0:
            raise SpawnRejected("delegation depth must be non-negative")
        if estimated_tokens < 0 or estimated_cost_usd < 0:
            raise SpawnRejected("estimated token/cost values must be non-negative")
        if self.remaining_wall_s() <= 0:
            raise SpawnRejected("root wall deadline exhausted")
        async with self._lock:
            if (
                self._used_tokens + self._reserved_tokens + estimated_tokens
                > self.budget.max_tokens
            ):
                raise SpawnRejected("token budget exhausted")
            if (
                self.budget.max_cost_usd is not None
                and self._used_cost + self._reserved_cost + estimated_cost_usd
                > self.budget.max_cost_usd
            ):
                raise SpawnRejected("cost budget exhausted")

    async def acquire(
        self,
        job_id: str,
        *,
        depth: int,
        estimated_tokens: int = 0,
        estimated_cost_usd: float = 0.0,
    ) -> SpawnReservation:
        await self.pre_check(
            depth=depth,
            estimated_tokens=estimated_tokens,
            estimated_cost_usd=estimated_cost_usd,
        )
        reservation = SpawnReservation(job_id, depth, estimated_tokens, estimated_cost_usd)
        async with self._lock:
            if job_id in self._leases and not self._leases[job_id].released:
                raise SpawnRejected(f"job already admitted: {job_id}")
            # ``pre_check`` is intentionally a cheap public probe.  A second
            # caller can pass that probe before this lock is acquired, so the
            # budget check must be repeated while reservations are serialized.
            if (
                self._used_tokens + self._reserved_tokens + estimated_tokens
                > self.budget.max_tokens
            ):
                raise SpawnRejected("token budget exhausted")
            if (
                self.budget.max_cost_usd is not None
                and self._used_cost + self._reserved_cost + estimated_cost_usd
                > self.budget.max_cost_usd
            ):
                raise SpawnRejected("cost budget exhausted")
            self._reserved_tokens += estimated_tokens
            self._reserved_cost += estimated_cost_usd
            self._leases[job_id] = reservation
            self._cancel_events.setdefault(job_id, asyncio.Event())
        try:
            await self._semaphore.acquire()
        except BaseException:
            await self.release(reservation, actual_tokens=0, actual_cost_usd=0.0)
            raise
        reservation.acquired = True
        reservation.acquired_at = self._clock()
        await self._emit({"type": "scheduler.slot_acquired", "job_id": job_id})
        return reservation

    async def release(
        self,
        reservation: SpawnReservation,
        *,
        actual_tokens: int | None = None,
        actual_cost_usd: float | None = None,
    ) -> None:
        if reservation.released:
            return
        async with self._lock:
            if reservation.released:
                return
            self._reserved_tokens = max(0, self._reserved_tokens - reservation.estimated_tokens)
            self._reserved_cost = max(0.0, self._reserved_cost - reservation.estimated_cost_usd)
            self._used_tokens += max(
                0, int(actual_tokens if actual_tokens is not None else reservation.estimated_tokens)
            )
            self._used_cost += max(
                0.0,
                float(
                    actual_cost_usd
                    if actual_cost_usd is not None
                    else reservation.estimated_cost_usd
                ),
            )
            reservation.released = True
            self._leases.pop(reservation.job_id, None)
            self._tasks.pop(reservation.job_id, None)
            self._cancel_events.pop(reservation.job_id, None)
        if reservation.acquired:
            self._semaphore.release()
            await self._emit({"type": "scheduler.slot_released", "job_id": reservation.job_id})

    @asynccontextmanager
    async def slot(
        self, job_id: str, *, depth: int, estimated_tokens: int = 0, estimated_cost_usd: float = 0.0
    ):
        reservation = await self.acquire(
            job_id,
            depth=depth,
            estimated_tokens=estimated_tokens,
            estimated_cost_usd=estimated_cost_usd,
        )
        try:
            yield reservation
        finally:
            await self.release(reservation)

    async def run(
        self,
        job_id: str,
        operation: Callable[[asyncio.Event], Awaitable[Any]],
        *,
        depth: int,
        estimated_tokens: int = 0,
        estimated_cost_usd: float = 0.0,
        timeout_s: float | None = None,
    ) -> Any:
        reservation = await self.acquire(
            job_id,
            depth=depth,
            estimated_tokens=estimated_tokens,
            estimated_cost_usd=estimated_cost_usd,
        )
        cancel_event = self._cancel_events[job_id]
        current = asyncio.current_task()
        if current is not None:
            self._tasks[job_id] = current
        actual_tokens: int | None = None
        actual_cost_usd: float | None = None
        try:
            timeout = timeout_s or self.budget.subagent_timeout_s
            result = await asyncio.wait_for(operation(cancel_event), timeout=timeout)
            if isinstance(result, dict):
                prompt_tokens = result.get("prompt_tokens")
                completion_tokens = result.get("completion_tokens")
                if prompt_tokens is not None or completion_tokens is not None:
                    actual_tokens = int(prompt_tokens or 0) + int(completion_tokens or 0)
                if result.get("cost_usd") is not None:
                    actual_cost_usd = float(result["cost_usd"] or 0.0)
            else:
                prompt_tokens = getattr(result, "prompt_tokens", None)
                completion_tokens = getattr(result, "completion_tokens", None)
                if prompt_tokens is not None or completion_tokens is not None:
                    actual_tokens = int(prompt_tokens or 0) + int(completion_tokens or 0)
                if getattr(result, "cost_usd", None) is not None:
                    actual_cost_usd = float(result.cost_usd or 0.0)
            return result
        finally:
            await self.release(
                reservation,
                actual_tokens=actual_tokens,
                actual_cost_usd=actual_cost_usd,
            )

    def cancel(self, job_id: str) -> bool:
        event = self._cancel_events.get(job_id)
        task = self._tasks.get(job_id)
        if event is None and task is None:
            return False
        if event is not None:
            event.set()
        if task is not None and not task.done():
            task.cancel()
        return True

    def snapshot(self) -> dict[str, Any]:
        return {
            "active": self.active_count,
            "queued": self.queued_count,
            "max_parallel": self.budget.max_parallel,
            "used_tokens": self._used_tokens,
            "reserved_tokens": self._reserved_tokens,
            "max_tokens": self.budget.max_tokens,
            "used_cost_usd": self._used_cost,
            "reserved_cost_usd": self._reserved_cost,
            "remaining_wall_s": self.remaining_wall_s(),
        }
