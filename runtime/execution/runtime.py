"""Optional application lifecycle integration for the durable repository."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .durable import DurableExecutionError, DurableExecutionRepository
from .outbox import OutboxPublisher
from .reconciler import Reconciler

logger = logging.getLogger("veya.execution.runtime")


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() not in {"0", "false", "off", "no"}


@dataclass(frozen=True)
class DurableRuntimeConfig:
    enabled: bool = False
    production: bool = False
    database_url: str | None = None
    sqlite_path: str = ".veya/execution-runtime.sqlite3"
    lease_ttl_s: float = 30.0
    heartbeat_interval_s: float = 10.0
    reconciliation_interval_s: float = 15.0
    reconciliation_batch_size: int = 100
    queue_read: bool = False
    queue_claim: bool = False
    lease_fencing: bool = True
    side_effect_ledger: bool = False
    reconciler_enabled: bool = False
    finalization_resume: bool = False
    event_outbox: bool = False
    shadow_compare: bool = False
    dual_run_compare: bool = False
    remote_worker_adapter: bool = False

    @classmethod
    def from_env(cls) -> DurableRuntimeConfig:
        enabled = _env_bool("VEYA_DURABLE_EXECUTION", False)
        return cls(
            enabled=enabled,
            production=_env_bool("VEYA_EXECUTION_PRODUCTION", False),
            database_url=os.environ.get("VEYA_EXECUTION_DATABASE_URL") or os.environ.get("DATABASE_URL"),
            sqlite_path=os.environ.get("VEYA_EXECUTION_SQLITE_PATH", ".veya/execution-runtime.sqlite3"),
            lease_ttl_s=float(os.environ.get("VEYA_EXECUTION_LEASE_TTL_S", "30")),
            heartbeat_interval_s=float(os.environ.get("VEYA_EXECUTION_HEARTBEAT_INTERVAL_S", "10")),
            reconciliation_interval_s=float(os.environ.get("VEYA_EXECUTION_RECONCILIATION_INTERVAL_S", "15")),
            reconciliation_batch_size=int(os.environ.get("VEYA_EXECUTION_RECONCILIATION_BATCH_SIZE", "100")),
            queue_read=_env_bool("VEYA_EXECUTION_DURABLE_QUEUE_READ", enabled),
            queue_claim=_env_bool("VEYA_EXECUTION_DURABLE_QUEUE_CLAIM", enabled),
            lease_fencing=_env_bool("VEYA_EXECUTION_LEASE_FENCING", True),
            side_effect_ledger=_env_bool("VEYA_EXECUTION_SIDE_EFFECT_LEDGER", enabled),
            reconciler_enabled=_env_bool("VEYA_EXECUTION_RECONCILER", enabled),
            finalization_resume=_env_bool("VEYA_EXECUTION_FINALIZATION_RESUME", enabled),
            event_outbox=_env_bool("VEYA_EXECUTION_EVENT_OUTBOX", enabled),
            shadow_compare=_env_bool("VEYA_EXECUTION_SHADOW_COMPARE", False),
            dual_run_compare=_env_bool("VEYA_EXECUTION_DUAL_RUN_COMPARE", False),
            remote_worker_adapter=_env_bool("VEYA_EXECUTION_REMOTE_WORKER_ADAPTER", False),
        )

    def validate(self) -> None:
        if self.lease_ttl_s <= 0 or self.heartbeat_interval_s <= 0 or self.heartbeat_interval_s * 2 > self.lease_ttl_s:
            raise DurableExecutionError("CONFIG_INVALID", "lease TTL must be at least 2x heartbeat interval")
        if self.reconciliation_interval_s <= 0 or self.reconciliation_batch_size <= 0:
            raise DurableExecutionError("CONFIG_INVALID", "reconciliation settings must be positive")
        if self.enabled and not self.event_outbox:
            raise DurableExecutionError("CONFIG_INVALID", "durable execution requires event outbox")
        if self.enabled and self.production and (
            not self.database_url
            or not self.database_url.startswith(("postgres://", "postgresql://"))
        ):
            raise DurableExecutionError(
                "CONFIG_INVALID",
                "production durable execution requires a PostgreSQL DSN; SQLite is local/test only",
            )


class DurableExecutionRuntime:
    def __init__(self, config: DurableRuntimeConfig | None = None):
        self.config = config or DurableRuntimeConfig.from_env()
        self.config.validate()
        self.repository = DurableExecutionRepository(
            dsn=self.config.database_url,
            sqlite_path=Path(self.config.sqlite_path),
            production=self.config.production,
        )
        self.reconciler = Reconciler(
            self.repository,
            interval_s=self.config.reconciliation_interval_s,
            batch_size=self.config.reconciliation_batch_size,
            on_report=self._record_reconciliation,
        )
        self._reconciler_task: asyncio.Task[Any] | None = None
        self._outbox_publisher: OutboxPublisher | None = None
        self._outbox_task: asyncio.Task[Any] | None = None
        self._started = False
        self._last_reconciliation_at: float | None = None
        self._last_reconciliation: dict[str, Any] | None = None

    async def _record_reconciliation(self, report: Any) -> None:
        self._last_reconciliation_at = time.time()
        self._last_reconciliation = report.to_dict()

    async def start(self) -> dict[str, Any]:
        if not self.config.enabled:
            return {"ok": True, "enabled": False, "backend": "disabled"}
        if self._started:
            return await self.health()
        self.reconciler.reset()
        await self.repository.connect()
        # Startup reconciliation completes before the periodic scanner is
        # launched, so this process cannot claim a scope before recovery runs.
        await self.reconciler.startup()
        if self.config.reconciler_enabled:
            self._reconciler_task = asyncio.create_task(self.reconciler.run(), name="veya-execution-reconciler")
        self._outbox_publisher = OutboxPublisher(
            self.repository,
            self._publish_event,
            interval_s=1.0,
            batch_size=100,
        )
        self._outbox_task = asyncio.create_task(self._outbox_publisher.run(), name="veya-execution-outbox")
        self._started = True
        return await self.health()

    @staticmethod
    async def _publish_event(event: dict[str, Any]) -> None:
        """Project committed durable events into Veya's existing EventStore.

        The durable event log remains authoritative.  EventStore is only the
        existing SSE/replay projection, and its event-id deduplication makes
        publisher retries safe.
        """
        from server.events import event_store

        event_store.append(
            {
                "event_id": event.get("event_id"),
                "topic": event.get("event_type") or event.get("topic") or "execution.updated",
                "session_id": event.get("goal_run_id") or "unknown",
                "trace_id": event.get("trace_id") or event.get("goal_run_id") or "unknown",
                "task_id": event.get("goal_run_id"),
                "actor": "runtime",
                "ts": event.get("occurred_at"),
                "payload": event.get("payload") or {},
            }
        )

    async def close(self) -> None:
        self.reconciler.stop()
        if self._outbox_publisher is not None:
            self._outbox_publisher.stop()
        if self._outbox_task is not None:
            self._outbox_task.cancel()
            await asyncio.gather(self._outbox_task, return_exceptions=True)
            self._outbox_task = None
            self._outbox_publisher = None
        if self._reconciler_task is not None:
            self._reconciler_task.cancel()
            await asyncio.gather(self._reconciler_task, return_exceptions=True)
            self._reconciler_task = None
        await self.repository.close()
        self._started = False

    async def health(self) -> dict[str, Any]:
        if not self.config.enabled:
            return {
                "ok": True,
                "healthy": True,
                "enabled": False,
                "backend": "disabled",
                "authority": "disabled",
            }
        value = await self.repository.health()
        with_metrics = dict(value)
        if value.get("ok"):
            with_metrics["metrics"] = await self.repository.metrics()
        return {
            **with_metrics,
            "healthy": bool(with_metrics.get("ok")) and self._started,
            "enabled": True,
            "started": self._started,
            "reconciler": self.config.reconciler_enabled,
            "reconciler_last_success": self._last_reconciliation_at,
            "reconciler_last_report": self._last_reconciliation,
        }


_default_runtime: DurableExecutionRuntime | None = None


def get_durable_runtime() -> DurableExecutionRuntime:
    global _default_runtime
    if _default_runtime is None:
        _default_runtime = DurableExecutionRuntime()
    return _default_runtime
