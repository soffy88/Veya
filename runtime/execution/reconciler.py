"""Startup and periodic durable execution reconciliation."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from .durable import DurableExecutionRepository, ReconciliationReport


class Reconciler:
    """Run bounded, repeatable lease/recovery scans for one repository."""

    def __init__(
        self,
        repository: DurableExecutionRepository,
        *,
        interval_s: float = 15.0,
        batch_size: int = 100,
        on_report: Callable[[ReconciliationReport], Any] | None = None,
    ):
        self.repository = repository
        self.interval_s = max(1.0, interval_s)
        self.batch_size = max(1, batch_size)
        self.on_report = on_report
        self._stop = asyncio.Event()

    async def run_once(self, scope: str | None = None) -> ReconciliationReport:
        report = await self.repository.reconcile(scope, limit=self.batch_size)
        if self.on_report is not None:
            result = self.on_report(report)
            if asyncio.iscoroutine(result):
                await result
        return report

    async def startup(self, scope: str | None = None) -> ReconciliationReport:
        """Perform the mandatory startup pass before a worker claims work."""
        return await self.run_once(scope)

    def reset(self) -> None:
        """Allow a runtime instance to be started again after a clean close."""
        self._stop.clear()

    async def run(self, scope: str | None = None) -> None:
        await self.startup(scope)
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval_s)
            except TimeoutError:
                await self.run_once(scope)

    def stop(self) -> None:
        self._stop.set()
