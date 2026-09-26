"""Event Subscription Engine for Veya Agent Runtime V1.

Subscribes to VeyaEvents and converts matching events into AgentTriggers.
Never runs executors directly.
"""

from __future__ import annotations

import time
import uuid
from pathlib import Path
from typing import Any

from veya.remote.events import VeyaEvent

from .models import AgentTrigger, EventSubscription, TriggerType
from .store import AgentRuntimeStore
from .trigger import TriggerManager


class EventSubscriptionEngine:
    """Matches VeyaEvents against subscriptions and queues AgentTriggers."""

    def __init__(
        self,
        project_root: str | Path,
        store: AgentRuntimeStore | None = None,
        trigger_manager: TriggerManager | None = None,
    ) -> None:
        self.project_root = Path(project_root)
        self.store = store or AgentRuntimeStore(project_root)
        self.triggers = trigger_manager or TriggerManager(project_root, self.store)

    def subscribe(
        self,
        channel_id: str,
        event_types: list[str],
        *,
        principal_id: str = "default",
        filters: dict[str, Any] | None = None,
        target: str = "mission",
        enabled: bool = True,
    ) -> EventSubscription:
        """Create a new event subscription."""
        sub = EventSubscription(
            subscription_id=f"sub_{uuid.uuid4().hex[:12]}",
            channel_id=channel_id,
            principal_id=principal_id,
            event_types=[str(t) for t in event_types],
            filters=dict(filters or {}),
            target=target,
            enabled=enabled,
            created_at=time.time(),
        )
        self.store.save_subscription(sub)
        return sub

    def matches(self, sub: EventSubscription, event: VeyaEvent) -> bool:
        """Check if subscription matches the event."""
        if not sub.enabled:
            return False

        # Match event type
        if "*" not in sub.event_types and str(event.event_type) not in sub.event_types:
            return False

        # Match filters against event payload / fields
        for k, expected in sub.filters.items():
            if k == "mission_id" and event.mission_id != expected:
                return False
            if k == "execution_id" and event.execution_id != expected:
                return False
            if k == "actor" and event.actor != expected:
                return False
            if k in event.payload and event.payload[k] != expected:
                return False
            if k not in event.payload and k not in ("mission_id", "execution_id", "actor"):
                return False

        return True

    def process_event(self, event: VeyaEvent) -> list[AgentTrigger]:
        """Evaluate event against all subscriptions and generate triggers."""
        subscriptions = self.store.list_subscriptions()
        generated: list[AgentTrigger] = []

        for sub in subscriptions:
            if not self.matches(sub, event):
                continue

            idempotency_key = f"evt_{event.event_id}_{sub.subscription_id}"
            trigger = self.triggers.create_trigger(
                channel_id=sub.channel_id,
                trigger_type=TriggerType.EVENT,
                principal_id=sub.principal_id,
                payload={
                    "event_id": event.event_id,
                    "event_type": str(event.event_type),
                    "execution_id": event.execution_id,
                    "mission_id": event.mission_id,
                    "subscription_id": sub.subscription_id,
                    "target": sub.target,
                    "event_payload": dict(event.payload),
                },
                idempotency_key=idempotency_key,
            )
            generated.append(trigger)

        return generated
