"""Durable outbox publisher with at-least-once delivery."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from .durable import DurableExecutionRepository

logger = logging.getLogger("veya.execution.outbox")


class OutboxPublisher:
    def __init__(
        self,
        repository: DurableExecutionRepository,
        publish: Callable[[dict[str, Any]], Awaitable[None] | None],
        *,
        interval_s: float = 1.0,
        batch_size: int = 100,
    ):
        self.repository = repository
        self.publish = publish
        self.interval_s = max(0.05, interval_s)
        self.batch_size = max(1, batch_size)
        self._stop = asyncio.Event()

    async def run_once(self) -> dict[str, int]:
        return await self.repository.publish_outbox(self.publish, limit=self.batch_size)

    async def run(self) -> None:
        while not self._stop.is_set():
            try:
                result = await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                # Publication is at-least-once and the database remains the
                # source of truth.  A transient DB/publisher outage must not
                # permanently kill the background loop.
                logger.exception("durable outbox publish pass failed")
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._stop.wait(), timeout=self.interval_s)
                continue
            if result["published"]:
                continue
            if result["failed"]:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._stop.wait(), timeout=self.interval_s)
                continue
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval_s)

    def stop(self) -> None:
        self._stop.set()
