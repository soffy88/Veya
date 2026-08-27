"""WorkerHost for the durable claim/lease/attempt protocol."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import os
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from .durable import ClaimEnvelope, DurableExecutionError, DurableExecutionRepository

WorkerCallback = Callable[
    [ClaimEnvelope, asyncio.Event], Awaitable[dict[str, Any]] | dict[str, Any]
]


class WorkerHost:
    """Execute claimed items without bypassing repository ownership checks."""

    def __init__(
        self,
        repository: DurableExecutionRepository,
        *,
        worker_id: str | None = None,
        callbacks: dict[str, WorkerCallback] | None = None,
        capabilities: set[str] | None = None,
        lease_ttl_s: float = 30.0,
        heartbeat_interval_s: float = 10.0,
    ):
        self.repository = repository
        self.incarnation_id = str(uuid.uuid4())
        self.worker_id = worker_id or f"execution/{os.uname().nodename}/{self.incarnation_id}"
        self.callbacks = dict(callbacks or {})
        self.capabilities = set(capabilities or callbacks or {"*"})
        self.lease_ttl_s = max(1.0, lease_ttl_s)
        self.heartbeat_interval_s = max(0.25, min(heartbeat_interval_s, self.lease_ttl_s / 2))
        self._draining = False
        self._active_cancel_events: dict[str, asyncio.Event] = {}

    async def start(self) -> dict[str, Any]:
        return await self.repository.register_worker(
            worker_id=self.worker_id, incarnation_id=self.incarnation_id
        )

    async def stop(self) -> None:
        self._draining = True
        for event in self._active_cancel_events.values():
            event.set()
        await self.repository.drain_worker(self.worker_id)

    async def run_once(self) -> bool:
        if self._draining:
            return False
        claim = await self.repository.claim_next(
            self.worker_id,
            capabilities=self.capabilities,
            lease_ttl_s=self.lease_ttl_s,
            process_id=self.incarnation_id,
        )
        if claim is None:
            return False
        callback = self.callbacks.get(claim.kind) or self.callbacks.get("*")
        if callback is None:
            await self.repository.fail(
                claim,
                {"message": f"no callback for kind={claim.kind}"},
                classification="permanent_failure",
            )
            return True
        await self.repository.start(claim)
        cancel_event = asyncio.Event()
        self._active_cancel_events[claim.work_item_id] = cancel_event
        heartbeat_failed = asyncio.Event()
        heartbeat_error: list[BaseException] = []

        async def heartbeat_loop() -> None:
            while not cancel_event.is_set():
                try:
                    await asyncio.wait_for(cancel_event.wait(), timeout=self.heartbeat_interval_s)
                except TimeoutError:
                    try:
                        await self.repository.heartbeat(
                            claim, {"worker_id": self.worker_id}, lease_ttl_s=self.lease_ttl_s
                        )
                    except DurableExecutionError as exc:
                        if exc.code == "STALE_FENCE":
                            heartbeat_failed.set()
                            cancel_event.set()
                            return
                        heartbeat_error.append(exc)
                        cancel_event.set()
                        return
                    except Exception as exc:
                        heartbeat_error.append(exc)
                        cancel_event.set()
                        return

        heartbeat_task = asyncio.create_task(
            heartbeat_loop(), name=f"veya-heartbeat-{claim.work_item_id}"
        )
        try:
            result = callback(claim, cancel_event)
            if inspect.isawaitable(result):
                result = await result
            if heartbeat_failed.is_set():
                raise DurableExecutionError("STALE_FENCE", "worker was fenced during execution")
            if heartbeat_error:
                raise DurableExecutionError(
                    "HEARTBEAT_FAILED", str(heartbeat_error[0])
                ) from heartbeat_error[0]
            await self.repository.complete(
                claim, result if isinstance(result, dict) else {"summary": str(result)}
            )
        except asyncio.CancelledError:
            cancel_event.set()
            with contextlib.suppress(DurableExecutionError):
                await self.repository.fail(
                    claim, {"message": "worker cancelled"}, classification="cancelled"
                )
            raise
        except DurableExecutionError as exc:
            if exc.code == "STALE_FENCE":
                # The old owner cannot mutate the item.  Reconciler will
                # classify any possible side effect from durable evidence.
                return True
            classification = getattr(exc, "classification", "permanent_failure")
            await self.repository.fail(
                claim, {"message": str(exc), "code": exc.code}, classification=classification
            )
        except Exception as exc:
            await self.repository.fail(
                claim,
                {"message": f"{type(exc).__name__}: {exc}"},
                classification="permanent_failure",
            )
        finally:
            cancel_event.set()
            self._active_cancel_events.pop(claim.work_item_id, None)
            heartbeat_task.cancel()
            await asyncio.gather(heartbeat_task, return_exceptions=True)
        return True

    async def run(
        self, stop_event: asyncio.Event | None = None, *, idle_sleep_s: float = 0.25
    ) -> None:
        await self.start()
        stop_event = stop_event or asyncio.Event()
        while not stop_event.is_set() and not self._draining:
            did_work = await self.run_once()
            if not did_work:
                await asyncio.sleep(max(0.01, idle_sleep_s))
