"""Continuous ready-queue scheduling with explicit parallel safety markers."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from .models import ExecutionSchedulerState


@dataclass
class SchedulerRun:
    state: ExecutionSchedulerState
    results: dict[str, Any] = field(default_factory=dict)
    errors: dict[str, BaseException] = field(default_factory=dict)


class ContinuousReadyScheduler:
    """Run ready work continuously, without a batch barrier.

    Items need ``id``, optional ``depends_on`` and optional ``parallel``
    attributes.  A false/missing ``parallel`` marker is exclusive and is never
    run beside another item.
    """

    def __init__(self, max_parallel: int = 4):
        if max_parallel < 1:
            raise ValueError("max_parallel must be at least 1")
        self.max_parallel = max_parallel

    async def run(
        self,
        items: Iterable[Any],
        execute: Callable[[Any], Awaitable[Any]],
        *,
        on_event: Callable[[dict[str, Any]], Any] | None = None,
        cancel_event: asyncio.Event | None = None,
    ) -> SchedulerRun:
        ordered = list(items)
        by_id = {str(item.id): item for item in ordered}
        if len(by_id) != len(ordered):
            raise ValueError("scheduler item ids must be unique")
        state = ExecutionSchedulerState(available_slots=self.max_parallel)
        state.queued = [str(item.id) for item in ordered]
        completed: set[str] = set()
        failed: set[str] = set()
        started: set[str] = set()
        results: dict[str, Any] = {}
        errors: dict[str, BaseException] = {}
        # The state projection and the scheduler loop must share one mapping;
        # otherwise the loop can observe an empty runtime while the snapshot
        # already contains started work and incorrectly mark it unreachable.
        running = state.running

        async def emit(event: dict[str, Any]) -> None:
            if on_event is None:
                return
            value = on_event(event)
            if asyncio.iscoroutine(value):
                await value

        def ready_items() -> list[Any]:
            return [
                item
                for item in ordered
                if str(item.id) not in started
                and all(str(dep) in completed for dep in (getattr(item, "depends_on", None) or []))
            ]

        def can_start(item: Any) -> bool:
            if not running:
                return True
            return bool(getattr(item, "parallel", False)) and all(
                bool(getattr(by_id[running_id], "parallel", False)) for running_id in running
            )

        async def start(item: Any) -> None:
            item_id = str(item.id)
            started.add(item_id)
            state.queued = [value for value in state.queued if value != item_id]
            state.running[item_id] = asyncio.create_task(execute(item), name=f"veya-runtime-{item_id}")
            state.available_slots = self.max_parallel - len(state.running)
            await emit({"type": "scheduler.task_started", "task_id": item_id})

        async def finish(done: asyncio.Task[Any], item_id: str) -> None:
            state.running.pop(item_id, None)
            state.available_slots = self.max_parallel - len(state.running)
            try:
                results[item_id] = done.result()
            except asyncio.CancelledError as exc:
                errors[item_id] = exc
                failed.add(item_id)
                await emit({"type": "delegate.cancelled", "task_id": item_id})
            except BaseException as exc:  # keep sibling results and continue fan-in
                errors[item_id] = exc
                failed.add(item_id)
                await emit({"type": "delegate.failed", "task_id": item_id, "error": str(exc)})
            else:
                completed.add(item_id)
                await emit({"type": "delegate.completed", "task_id": item_id})

        while len(started) < len(ordered) or running:
            if cancel_event is not None and cancel_event.is_set():
                for task in running.values():
                    task.cancel()
                state.finalizing = True
            if not state.finalizing:
                for item in ready_items():
                    if len(running) >= self.max_parallel or not can_start(item):
                        break
                    await start(item)
                    # A non-parallel item is always exclusive.
                    if not getattr(item, "parallel", False):
                        break
            if not running:
                pending = [item for item in ordered if str(item.id) not in started]
                if pending:
                    # Dependencies on failed/unknown nodes can never become ready.
                    for item in pending:
                        item_id = str(item.id)
                        started.add(item_id)
                        failed.add(item_id)
                        errors[item_id] = RuntimeError("dependencies cannot be satisfied")
                        await emit({"type": "delegate.failed", "task_id": item_id, "error": "unreachable"})
                    continue
                break
            done, _ = await asyncio.wait(list(state.running.values()), return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                item_id = next(key for key, value in list(state.running.items()) if value is task)
                await finish(task, item_id)

        state.completed = completed
        state.failed = failed
        state.available_slots = self.max_parallel - len(state.running)
        return SchedulerRun(state=state, results=results, errors=errors)
