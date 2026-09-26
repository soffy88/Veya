"""Notification Outbox, Delivery Worker, and Circuit Breaker for Veya Agent Runtime V1.

Ensures:
- Reliable outward notification delivery
- Exponential backoff with jitter on transient failures
- Circuit breaker to protect degraded sinks from retry storms
- Dead-lettering with durable replay capability
"""

from __future__ import annotations

import inspect
import random
import time
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from veya.channel.notification import Notification

from .models import (
    CircuitBreakerState,
    DeadLetterRecord,
    DeliveryStatus,
    NotificationDelivery,
)
from .store import AgentRuntimeStore

DeliveryHandler = Callable[[dict[str, Any]], Awaitable[None] | None]


class CircuitBreaker:
    """Monitors sink health to prevent retry storms."""

    def __init__(
        self,
        failure_threshold: int = 3,
        recovery_timeout: float = 30.0,
    ) -> None:
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self._states: dict[str, CircuitBreakerState] = {}
        self._failure_counts: dict[str, int] = {}
        self._trip_times: dict[str, float] = {}

    def get_state(self, sink_id: str) -> CircuitBreakerState:
        state = self._states.get(sink_id, CircuitBreakerState.CLOSED)
        if state == CircuitBreakerState.OPEN:
            trip_time = self._trip_times.get(sink_id, 0.0)
            if time.time() - trip_time >= self.recovery_timeout:
                self._states[sink_id] = CircuitBreakerState.HALF_OPEN
                return CircuitBreakerState.HALF_OPEN
        return state

    def can_attempt(self, sink_id: str) -> bool:
        return self.get_state(sink_id) != CircuitBreakerState.OPEN

    def record_success(self, sink_id: str) -> None:
        self._states[sink_id] = CircuitBreakerState.CLOSED
        self._failure_counts[sink_id] = 0
        if sink_id in self._trip_times:
            del self._trip_times[sink_id]

    def record_failure(self, sink_id: str) -> None:
        counts = self._failure_counts.get(sink_id, 0) + 1
        self._failure_counts[sink_id] = counts
        if counts >= self.failure_threshold:
            self._states[sink_id] = CircuitBreakerState.OPEN
            self._trip_times[sink_id] = time.time()

    def all_states(self) -> dict[str, str]:
        return {sink: str(self.get_state(sink)) for sink in self._failure_counts}


class NotificationOutbox:
    """Durable notification outbox and delivery dispatcher."""

    def __init__(
        self,
        project_root: str | Path,
        store: AgentRuntimeStore | None = None,
        circuit_breaker: CircuitBreaker | None = None,
    ) -> None:
        self.project_root = Path(project_root)
        self.store = store or AgentRuntimeStore(project_root)
        self.circuit_breaker = circuit_breaker or CircuitBreaker()
        self._handlers: dict[str, DeliveryHandler] = {}

    def register_sink(self, sink_id: str, handler: DeliveryHandler) -> None:
        self._handlers[sink_id] = handler

    def enqueue(
        self,
        notification: Notification,
        sink_ids: list[str] | None = None,
    ) -> list[NotificationDelivery]:
        """Enqueue notification for delivery across target sinks."""
        targets = sink_ids if sink_ids else (list(self._handlers.keys()) or ["default"])
        deliveries: list[NotificationDelivery] = []
        now = time.time()

        for sink_id in targets:
            deliv = NotificationDelivery(
                delivery_id=f"deliv_{uuid.uuid4().hex[:12]}",
                notification_id=notification.notification_id,
                channel_id=notification.channel_id,
                sink_id=sink_id,
                payload=notification.to_dict(),
                status=DeliveryStatus.PENDING,
                attempt=0,
                max_attempts=5,
                next_retry_at=now,
                created_at=now,
            )
            self.store.save_delivery(deliv)
            deliveries.append(deliv)

        return deliveries

    async def deliver_single(self, delivery: NotificationDelivery) -> bool:
        """Attempt to deliver a single item."""
        if not self.circuit_breaker.can_attempt(delivery.sink_id):
            return False

        handler = self._handlers.get(delivery.sink_id)
        delivery.status = DeliveryStatus.SENDING
        self.store.save_delivery(delivery)

        try:
            if handler:
                res = handler(delivery.payload)
                if inspect.isawaitable(res):
                    await res

            delivery.status = DeliveryStatus.DELIVERED
            delivery.delivered_at = time.time()
            delivery.last_error = None
            self.circuit_breaker.record_success(delivery.sink_id)
            self.store.save_delivery(delivery)
            return True
        except Exception as exc:
            err_msg = str(exc)
            delivery.attempt += 1
            delivery.last_error = err_msg
            self.circuit_breaker.record_failure(delivery.sink_id)

            if delivery.attempt >= delivery.max_attempts:
                delivery.status = DeliveryStatus.DEAD_LETTERED
                self.store.save_delivery(delivery)
                self.store.save_dead_letter(
                    DeadLetterRecord(
                        dead_letter_id=f"dl_{uuid.uuid4().hex[:12]}",
                        entity_type="delivery",
                        entity_id=delivery.delivery_id,
                        channel_id=delivery.channel_id,
                        payload=delivery.payload,
                        reason="max_delivery_attempts_exhausted",
                        error_detail=err_msg,
                        original_idempotency_key=delivery.notification_id,
                        attempts=delivery.attempt,
                        created_at=time.time(),
                    )
                )
            else:
                delivery.status = DeliveryStatus.RETRYING
                # Exponential backoff with jitter
                backoff = min(60.0, (2**delivery.attempt) + random.uniform(0.1, 0.5))
                delivery.next_retry_at = time.time() + backoff
                self.store.save_delivery(delivery)

            return False

    async def process_outbox(self) -> int:
        """Poll and process all due deliveries."""
        now = time.time()
        deliveries = self.store.list_deliveries()
        processed = 0

        for d in deliveries:
            if (
                d.status in (DeliveryStatus.PENDING, DeliveryStatus.RETRYING)
                and d.next_retry_at <= now
            ):
                await self.deliver_single(d)
                processed += 1

        return processed

    def replay_dead_letter(self, dead_letter_id: str) -> NotificationDelivery | None:
        """Replay dead-lettered delivery back to outbox."""
        dl = self.store.get_dead_letter(dead_letter_id)
        if not dl or dl.entity_type != "delivery":
            return None

        deliv = self.store.get_delivery(dl.entity_id)
        if not deliv:
            return None

        deliv.status = DeliveryStatus.PENDING
        deliv.attempt = 0
        deliv.next_retry_at = time.time()
        deliv.last_error = None
        self.store.save_delivery(deliv)
        return deliv
