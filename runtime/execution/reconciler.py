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
        abandon_after_s: float = 0.0,
        worker_ttl_s: float = 0.0,
    ):
        self.repository = repository
        # 0 disables the sweep (the default for direct/test construction).
        self.abandon_after_s = max(0.0, float(abandon_after_s))
        self.worker_ttl_s = max(0.0, float(worker_ttl_s))
        self.last_abandoned: list[str] = []
        self.last_pruned_workers: list[str] = []
        self.interval_s = max(1.0, interval_s)
        self.batch_size = max(1, batch_size)
        self.on_report = on_report
        self._stop = asyncio.Event()

    async def run_once(self, scope: str | None = None) -> ReconciliationReport:
        report = await self.repository.reconcile(scope, limit=self.batch_size)
        if self.abandon_after_s > 0 and scope is None:
            # After expired leases are classified: a run nobody can reclaim
            # any more is terminated instead of staying non-terminal forever.
            self.last_abandoned = await self.repository.close_abandoned_goal_runs(
                stale_after_s=self.abandon_after_s, limit=self.batch_size
            )
            if self.last_abandoned:
                report.scanned += len(self.last_abandoned)
                report.abandoned_closed += len(self.last_abandoned)
                for run_id in self.last_abandoned:
                    report.decisions.append({
                        "goal_run_id": run_id,
                        "decision": "ABANDONED_CLOSED",
                    })
        if self.worker_ttl_s > 0 and scope is None:
            self.last_pruned_workers = await self.repository.prune_stale_workers(
                stale_after_s=self.worker_ttl_s, limit=self.batch_size
            )
            if self.last_pruned_workers:
                report.scanned += len(self.last_pruned_workers)
                report.workers_pruned += len(self.last_pruned_workers)
                for w_id in self.last_pruned_workers:
                    report.decisions.append({
                        "worker_id": w_id,
                        "decision": "WORKER_PRUNED",
                    })
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
