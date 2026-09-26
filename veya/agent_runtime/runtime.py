"""Daemon Agent Runtime for Veya Agent Runtime V1.

The central long-running authority orchestrating:
- Startup recovery sequence
- Monotonic runtime generation tracking
- Safe lease reclamation and stale claim invalidation
- Scheduler ticks
- Admission-controlled trigger routing to Missions
- Notification outbox delivery
- Graceful drain and shutdown
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from pathlib import Path
from typing import Any

from veya.channel.inbox import AgentInbox, InboxMessageStatus
from veya.channel.store import ChannelStore
from veya.supervision.models import MissionStatus
from veya.supervision.store import MissionStore

from .admission import AdmissionController, AdmissionDecision
from .continuity import SessionContinuityManager
from .event_sub import EventSubscriptionEngine
from .models import (
    AdmissionPolicy,
    AgentRuntimeHealth,
    DeliveryStatus,
    RuntimeStatus,
    TriggerStatus,
    TriggerType,
)
from .outbox import CircuitBreaker, NotificationOutbox
from .router import MissionTriggerRouter
from .scheduler import AgentScheduler
from .store import AgentRuntimeStore
from .trigger import TriggerManager


class AgentRuntime:
    """Canonical Daemon Agent Runtime."""

    def __init__(
        self,
        project_root: str | Path,
        *,
        admission_policy: AdmissionPolicy | None = None,
        store: AgentRuntimeStore | None = None,
    ) -> None:
        self.project_root = Path(project_root)
        self.store = store or AgentRuntimeStore(self.project_root)
        self.channels = ChannelStore(self.project_root)
        self.inbox = AgentInbox(self.project_root)
        self.mission_store = MissionStore(self.project_root)

        self.triggers = TriggerManager(self.project_root, self.store)
        self.admission = AdmissionController(admission_policy)
        self.router = MissionTriggerRouter(
            self.project_root,
            self.store,
            self.mission_store,
            self.channels,
        )
        self.scheduler = AgentScheduler(self.project_root, self.store, self.triggers)
        self.events = EventSubscriptionEngine(self.project_root, self.store, self.triggers)
        self.circuit_breaker = CircuitBreaker()
        self.outbox = NotificationOutbox(self.project_root, self.store, self.circuit_breaker)
        self.continuity = SessionContinuityManager(self.project_root)

        self._status: RuntimeStatus = RuntimeStatus.STOPPED
        self._generation: int = 1
        self._started_at: float = 0.0
        self._worker_id: str = f"worker_{int(time.time())}"
        self._stop_event = asyncio.Event()

    @property
    def status(self) -> RuntimeStatus:
        return self._status

    @property
    def generation(self) -> int:
        return self._generation

    def startup_recovery(self) -> dict[str, Any]:
        """Execute the startup recovery sequence."""
        self._status = RuntimeStatus.STARTING
        self._started_at = time.time()

        # 1. Bump and load runtime generation
        self._generation = self.store.bump_generation()

        # 2. Reclaim stale leases
        reclaimed_leases = self.triggers.reclaim_stale_leases(self._generation)

        # 3. Recover pending un-dispatched inbox messages
        recovered_inbox = 0
        try:
            for ch in self.channels.list():
                pending_msgs = self.inbox.poll(ch.channel_id, status=InboxMessageStatus.PENDING)
                for msg in pending_msgs:
                    # Ingest into trigger queue with deduplication key
                    self.triggers.create_trigger(
                        channel_id=ch.channel_id,
                        trigger_type=TriggerType.USER_MESSAGE,
                        message_id=msg.message_id,
                        principal_id="user",
                        payload={"content": msg.content, **msg.payload},
                        idempotency_key=f"inbox_{msg.message_id}",
                        generation=self._generation,
                    )
                    recovered_inbox += 1
        except Exception:
            pass

        # 4. Count active missions
        active_missions = [
            m
            for m in self.mission_store.list()
            if m.status not in (MissionStatus.done, MissionStatus.failed, MissionStatus.cancelled)
        ]

        # 5. Check outbox pending count
        pending_outbox = self.store.list_deliveries(status=DeliveryStatus.PENDING)

        self._status = RuntimeStatus.RUNNING
        return {
            "generation": self._generation,
            "reclaimed_leases": reclaimed_leases,
            "recovered_inbox_messages": recovered_inbox,
            "active_missions": len(active_missions),
            "pending_outbox": len(pending_outbox),
        }

    async def step(self) -> dict[str, Any]:
        """Single execution cycle of the daemon runtime."""
        if self._status != RuntimeStatus.RUNNING:
            return {"status": str(self._status), "dispatched": 0, "outbox_processed": 0}

        now = time.time()

        # 1. Tick scheduler
        scheduled_triggers = self.scheduler.tick(now)

        # 2. Claim and process next trigger
        dispatched_count = 0
        trigger = self.triggers.claim_next_trigger(
            self._worker_id,
            ttl_seconds=30.0,
            current_generation=self._generation,
        )

        if trigger:
            target_mission_id = trigger.payload.get("mission_id")
            decision, reason = self.admission.evaluate_admission(
                trigger.principal_id,
                trigger.channel_id,
                target_mission_id,
            )

            if decision == AdmissionDecision.ADMITTED:
                try:
                    mission_id, _r_reason = self.router.route_trigger(trigger)
                    # If trigger was associated with inbox message, bind it
                    if trigger.message_id:
                        with contextlib.suppress(Exception):
                            self.inbox.bind_mission(
                                trigger.channel_id, trigger.message_id, mission_id
                            )

                    self.triggers.ack_trigger(
                        trigger.trigger_id,
                        trigger.lease_id or "",
                        status=TriggerStatus.DISPATCHED,
                        worker_generation=self._generation,
                    )
                    dispatched_count += 1
                except Exception as exc:
                    self.triggers.fail_trigger(
                        trigger.trigger_id,
                        trigger.lease_id or "",
                        str(exc),
                        worker_generation=self._generation,
                    )
                finally:
                    self.admission.release(
                        trigger.principal_id, trigger.channel_id, target_mission_id
                    )

            elif decision == AdmissionDecision.DEFERRED:
                # Capacity limit reached, release lease so it remains queued without failure
                trigger.status = TriggerStatus.PENDING
                trigger.lease_id = None
                trigger.lease_expires_at = None
                trigger.claimed_by = None
                self.store.save_trigger(trigger)

            elif decision == AdmissionDecision.REJECTED:
                self.triggers.fail_trigger(
                    trigger.trigger_id,
                    trigger.lease_id or "",
                    f"admission rejected: {reason}",
                )

        # 3. Process notification outbox deliveries
        outbox_count = await self.outbox.process_outbox()

        return {
            "status": str(self._status),
            "scheduled_triggers": len(scheduled_triggers),
            "dispatched": dispatched_count,
            "outbox_processed": outbox_count,
        }

    async def drain(self, timeout_seconds: float = 5.0) -> None:
        """Gracefully drain the daemon runtime."""
        self._status = RuntimeStatus.DRAINING
        end_time = time.time() + timeout_seconds

        # Drain outbox deliveries
        while time.time() < end_time:
            deliveries = self.store.list_deliveries(status=DeliveryStatus.PENDING)
            if not deliveries:
                break
            await self.outbox.process_outbox()
            await asyncio.sleep(0.05)

        # Release any held claims
        self.triggers.reclaim_stale_leases(self._generation + 1)
        self._status = RuntimeStatus.STOPPED

    def get_health(self) -> AgentRuntimeHealth:
        """Get instant health snapshot."""
        now = time.time()
        uptime = (now - self._started_at) if self._started_at > 0 else 0.0
        active_missions = [
            m
            for m in self.mission_store.list()
            if m.status not in (MissionStatus.done, MissionStatus.failed, MissionStatus.cancelled)
        ]
        pending_trigs = self.store.list_triggers(status=TriggerStatus.PENDING)
        pending_delivs = self.store.list_deliveries(status=DeliveryStatus.PENDING)
        dead_letters = self.store.list_dead_letters()

        return AgentRuntimeHealth(
            status=self._status,
            runtime_generation=self._generation,
            uptime_seconds=uptime,
            active_missions_count=len(active_missions),
            pending_triggers_count=len(pending_trigs),
            pending_deliveries_count=len(pending_delivs),
            dead_letters_count=len(dead_letters),
            circuit_breaker_states=self.circuit_breaker.all_states(),
            observed_at=now,
        )

    async def run_daemon(self, interval_seconds: float = 0.1) -> None:
        """Run long-lived background daemon event loop until cancelled or stopped."""
        import signal

        self.startup_recovery()
        loop = asyncio.get_running_loop()
        stop_fut = loop.create_future()

        def _sig_handler():
            if not stop_fut.done():
                stop_fut.set_result(None)

        for sig in (signal.SIGTERM, signal.SIGINT):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, _sig_handler)

        try:
            while not stop_fut.done() and not self._stop_event.is_set():
                await self.step()
                await asyncio.sleep(interval_seconds)
        finally:
            await self.drain()
