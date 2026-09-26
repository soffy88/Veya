"""Canonical Data Models for Veya Agent Runtime V1."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class RuntimeStatus(StrEnum):
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    DRAINING = "DRAINING"
    STOPPING = "STOPPING"
    STOPPED = "STOPPED"
    DEGRADED = "DEGRADED"


class TriggerType(StrEnum):
    USER_MESSAGE = "USER_MESSAGE"
    WEBHOOK = "WEBHOOK"
    SCHEDULE = "SCHEDULE"
    EVENT = "EVENT"
    SIGNAL = "SIGNAL"
    RETRY = "RETRY"
    SYSTEM = "SYSTEM"


class TriggerStatus(StrEnum):
    PENDING = "PENDING"
    CLAIMED = "CLAIMED"
    DISPATCHED = "DISPATCHED"
    PROCESSED = "PROCESSED"
    FAILED = "FAILED"
    DEAD_LETTERED = "DEAD_LETTERED"


@dataclass
class AgentTrigger:
    trigger_id: str
    channel_id: str
    trigger_type: TriggerType
    principal_id: str = "default"
    message_id: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    idempotency_key: str | None = None
    created_at: float = field(default_factory=time.time)
    available_at: float = field(default_factory=time.time)
    expires_at: float | None = None
    attempt: int = 0
    max_attempts: int = 3
    status: TriggerStatus = TriggerStatus.PENDING
    claimed_by: str | None = None
    lease_id: str | None = None
    lease_expires_at: float | None = None
    generation: int = 1
    last_error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "trigger_id": self.trigger_id,
            "channel_id": self.channel_id,
            "trigger_type": str(self.trigger_type),
            "principal_id": self.principal_id,
            "message_id": self.message_id,
            "payload": dict(self.payload),
            "idempotency_key": self.idempotency_key,
            "created_at": self.created_at,
            "available_at": self.available_at,
            "expires_at": self.expires_at,
            "attempt": self.attempt,
            "max_attempts": self.max_attempts,
            "status": str(self.status),
            "claimed_by": self.claimed_by,
            "lease_id": self.lease_id,
            "lease_expires_at": self.lease_expires_at,
            "generation": self.generation,
            "last_error": self.last_error,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AgentTrigger:
        return cls(
            trigger_id=str(data["trigger_id"]),
            channel_id=str(data["channel_id"]),
            trigger_type=TriggerType(str(data.get("trigger_type", "USER_MESSAGE")).upper()),
            principal_id=str(data.get("principal_id", "default")),
            message_id=data.get("message_id"),
            payload=dict(data.get("payload") or {}),
            idempotency_key=data.get("idempotency_key"),
            created_at=float(data.get("created_at", time.time())),
            available_at=float(data.get("available_at", time.time())),
            expires_at=float(data["expires_at"]) if data.get("expires_at") is not None else None,
            attempt=int(data.get("attempt", 0)),
            max_attempts=int(data.get("max_attempts", 3)),
            status=TriggerStatus(str(data.get("status", "PENDING")).upper()),
            claimed_by=data.get("claimed_by"),
            lease_id=data.get("lease_id"),
            lease_expires_at=float(data["lease_expires_at"])
            if data.get("lease_expires_at") is not None
            else None,
            generation=int(data.get("generation", 1)),
            last_error=data.get("last_error"),
        )


class ScheduleType(StrEnum):
    ONCE = "ONCE"
    INTERVAL = "INTERVAL"
    CRON = "CRON"


class MissedSchedulePolicy(StrEnum):
    SKIP = "SKIP"
    FIRE_ONCE = "FIRE_ONCE"
    CATCH_UP_LIMITED = "CATCH_UP_LIMITED"


@dataclass
class AgentSchedule:
    schedule_id: str
    channel_id: str
    principal_id: str
    schedule_type: ScheduleType
    expression: str
    payload: dict[str, Any] = field(default_factory=dict)
    enabled: bool = True
    missed_policy: MissedSchedulePolicy = MissedSchedulePolicy.FIRE_ONCE
    next_fire_at: float = 0.0
    last_fire_at: float | None = None
    last_trigger_id: str | None = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schedule_id": self.schedule_id,
            "channel_id": self.channel_id,
            "principal_id": self.principal_id,
            "schedule_type": str(self.schedule_type),
            "expression": self.expression,
            "payload": dict(self.payload),
            "enabled": self.enabled,
            "missed_policy": str(self.missed_policy),
            "next_fire_at": self.next_fire_at,
            "last_fire_at": self.last_fire_at,
            "last_trigger_id": self.last_trigger_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AgentSchedule:
        return cls(
            schedule_id=str(data["schedule_id"]),
            channel_id=str(data["channel_id"]),
            principal_id=str(data.get("principal_id", "default")),
            schedule_type=ScheduleType(str(data.get("schedule_type", "INTERVAL")).upper()),
            expression=str(data.get("expression", "60")),
            payload=dict(data.get("payload") or {}),
            enabled=bool(data.get("enabled", True)),
            missed_policy=MissedSchedulePolicy(str(data.get("missed_policy", "FIRE_ONCE")).upper()),
            next_fire_at=float(data.get("next_fire_at", 0.0)),
            last_fire_at=float(data["last_fire_at"])
            if data.get("last_fire_at") is not None
            else None,
            last_trigger_id=data.get("last_trigger_id"),
            created_at=float(data.get("created_at", time.time())),
            updated_at=float(data.get("updated_at", time.time())),
        )


@dataclass
class EventSubscription:
    subscription_id: str
    channel_id: str
    principal_id: str
    event_types: list[str] = field(default_factory=list)
    filters: dict[str, Any] = field(default_factory=dict)
    target: str = "mission"
    enabled: bool = True
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "subscription_id": self.subscription_id,
            "channel_id": self.channel_id,
            "principal_id": self.principal_id,
            "event_types": list(self.event_types),
            "filters": dict(self.filters),
            "target": self.target,
            "enabled": self.enabled,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> EventSubscription:
        return cls(
            subscription_id=str(data["subscription_id"]),
            channel_id=str(data["channel_id"]),
            principal_id=str(data.get("principal_id", "default")),
            event_types=[str(t) for t in data.get("event_types") or []],
            filters=dict(data.get("filters") or {}),
            target=str(data.get("target", "mission")),
            enabled=bool(data.get("enabled", True)),
            created_at=float(data.get("created_at", time.time())),
        )


class AdmissionPolicyMode(StrEnum):
    WAIT = "WAIT"
    REJECT = "REJECT"
    DEFER = "DEFER"


@dataclass
class AdmissionPolicy:
    global_concurrency: int = 10
    principal_concurrency: int = 5
    channel_concurrency: int = 5
    mission_concurrency: int = 3
    executor_concurrency: int = 10
    queue_depth: int = 100
    policy_mode: AdmissionPolicyMode = AdmissionPolicyMode.DEFER

    def to_dict(self) -> dict[str, Any]:
        return {
            "global_concurrency": self.global_concurrency,
            "principal_concurrency": self.principal_concurrency,
            "channel_concurrency": self.channel_concurrency,
            "mission_concurrency": self.mission_concurrency,
            "executor_concurrency": self.executor_concurrency,
            "queue_depth": self.queue_depth,
            "policy_mode": str(self.policy_mode),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AdmissionPolicy:
        return cls(
            global_concurrency=int(data.get("global_concurrency", 10)),
            principal_concurrency=int(data.get("principal_concurrency", 5)),
            channel_concurrency=int(data.get("channel_concurrency", 5)),
            mission_concurrency=int(data.get("mission_concurrency", 3)),
            executor_concurrency=int(data.get("executor_concurrency", 10)),
            queue_depth=int(data.get("queue_depth", 100)),
            policy_mode=AdmissionPolicyMode(str(data.get("policy_mode", "DEFER")).upper()),
        )


class DeliveryStatus(StrEnum):
    PENDING = "PENDING"
    SENDING = "SENDING"
    DELIVERED = "DELIVERED"
    RETRYING = "RETRYING"
    FAILED = "FAILED"
    DEAD_LETTERED = "DEAD_LETTERED"


class CircuitBreakerState(StrEnum):
    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"


@dataclass
class NotificationDelivery:
    delivery_id: str
    notification_id: str
    channel_id: str
    sink_id: str
    payload: dict[str, Any] = field(default_factory=dict)
    status: DeliveryStatus = DeliveryStatus.PENDING
    attempt: int = 0
    max_attempts: int = 5
    next_retry_at: float = 0.0
    last_error: str | None = None
    created_at: float = field(default_factory=time.time)
    delivered_at: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "delivery_id": self.delivery_id,
            "notification_id": self.notification_id,
            "channel_id": self.channel_id,
            "sink_id": self.sink_id,
            "payload": dict(self.payload),
            "status": str(self.status),
            "attempt": self.attempt,
            "max_attempts": self.max_attempts,
            "next_retry_at": self.next_retry_at,
            "last_error": self.last_error,
            "created_at": self.created_at,
            "delivered_at": self.delivered_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> NotificationDelivery:
        return cls(
            delivery_id=str(data["delivery_id"]),
            notification_id=str(data["notification_id"]),
            channel_id=str(data["channel_id"]),
            sink_id=str(data.get("sink_id", "default")),
            payload=dict(data.get("payload") or {}),
            status=DeliveryStatus(str(data.get("status", "PENDING")).upper()),
            attempt=int(data.get("attempt", 0)),
            max_attempts=int(data.get("max_attempts", 5)),
            next_retry_at=float(data.get("next_retry_at", 0.0)),
            last_error=data.get("last_error"),
            created_at=float(data.get("created_at", time.time())),
            delivered_at=float(data["delivered_at"])
            if data.get("delivered_at") is not None
            else None,
        )


@dataclass
class DeadLetterRecord:
    dead_letter_id: str
    entity_type: str  # "trigger" | "delivery" | "message"
    entity_id: str
    channel_id: str
    payload: dict[str, Any] = field(default_factory=dict)
    reason: str = ""
    error_detail: str = ""
    original_idempotency_key: str | None = None
    attempts: int = 0
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "dead_letter_id": self.dead_letter_id,
            "entity_type": self.entity_type,
            "entity_id": self.entity_id,
            "channel_id": self.channel_id,
            "payload": dict(self.payload),
            "reason": self.reason,
            "error_detail": self.error_detail,
            "original_idempotency_key": self.original_idempotency_key,
            "attempts": self.attempts,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DeadLetterRecord:
        return cls(
            dead_letter_id=str(data["dead_letter_id"]),
            entity_type=str(data.get("entity_type", "trigger")),
            entity_id=str(data.get("entity_id", "")),
            channel_id=str(data.get("channel_id", "")),
            payload=dict(data.get("payload") or {}),
            reason=str(data.get("reason", "")),
            error_detail=str(data.get("error_detail", "")),
            original_idempotency_key=data.get("original_idempotency_key"),
            attempts=int(data.get("attempts", 0)),
            created_at=float(data.get("created_at", time.time())),
        )


@dataclass
class AgentRuntimeHealth:
    status: RuntimeStatus
    runtime_generation: int
    uptime_seconds: float
    active_missions_count: int
    pending_triggers_count: int
    pending_deliveries_count: int
    dead_letters_count: int
    circuit_breaker_states: dict[str, str] = field(default_factory=dict)
    observed_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": str(self.status),
            "runtime_generation": self.runtime_generation,
            "uptime_seconds": round(self.uptime_seconds, 2),
            "active_missions_count": self.active_missions_count,
            "pending_triggers_count": self.pending_triggers_count,
            "pending_deliveries_count": self.pending_deliveries_count,
            "dead_letters_count": self.dead_letters_count,
            "circuit_breaker_states": dict(self.circuit_breaker_states),
            "observed_at": self.observed_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AgentRuntimeHealth:
        return cls(
            status=RuntimeStatus(str(data.get("status", "STOPPED")).upper()),
            runtime_generation=int(data.get("runtime_generation", 1)),
            uptime_seconds=float(data.get("uptime_seconds", 0.0)),
            active_missions_count=int(data.get("active_missions_count", 0)),
            pending_triggers_count=int(data.get("pending_triggers_count", 0)),
            pending_deliveries_count=int(data.get("pending_deliveries_count", 0)),
            dead_letters_count=int(data.get("dead_letters_count", 0)),
            circuit_breaker_states=dict(data.get("circuit_breaker_states") or {}),
            observed_at=float(data.get("observed_at", time.time())),
        )
