"""Veya Agent Runtime V1 package.

Situates above Mission and Execution Contract to provide durable, crash-resilient,
long-running daemon operations, trigger routing, admission control, scheduling,
and notification delivery.
"""

from __future__ import annotations

from .admission import AdmissionController, AdmissionDecision
from .continuity import SessionCheckpoint, SessionContinuityManager
from .event_sub import EventSubscriptionEngine
from .models import (
    AdmissionPolicy,
    AdmissionPolicyMode,
    AgentRuntimeHealth,
    AgentSchedule,
    AgentTrigger,
    CircuitBreakerState,
    DeadLetterRecord,
    DeliveryStatus,
    EventSubscription,
    MissedSchedulePolicy,
    NotificationDelivery,
    RuntimeStatus,
    ScheduleType,
    TriggerStatus,
    TriggerType,
)
from .outbox import CircuitBreaker, NotificationOutbox
from .router import MissionRoutingReason, MissionTriggerRouter
from .runtime import AgentRuntime
from .scheduler import AgentScheduler
from .store import AgentRuntimeStore
from .trigger import TriggerManager

__all__ = [
    "AdmissionController",
    "AdmissionDecision",
    "AdmissionPolicy",
    "AdmissionPolicyMode",
    "AgentRuntime",
    "AgentRuntimeHealth",
    "AgentRuntimeStore",
    "AgentSchedule",
    "AgentScheduler",
    "AgentTrigger",
    "CircuitBreaker",
    "CircuitBreakerState",
    "DeadLetterRecord",
    "DeliveryStatus",
    "EventSubscription",
    "EventSubscriptionEngine",
    "MissedSchedulePolicy",
    "MissionRoutingReason",
    "MissionTriggerRouter",
    "NotificationDelivery",
    "NotificationOutbox",
    "RuntimeStatus",
    "ScheduleType",
    "SessionCheckpoint",
    "SessionContinuityManager",
    "TriggerManager",
    "TriggerStatus",
    "TriggerType",
]
