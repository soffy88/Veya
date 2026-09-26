"""Trigger Queue and Durable Lease Protocol for Veya Agent Runtime V1.

Implements:
- Trigger lifecycle (PENDING -> CLAIMED -> DISPATCHED/PROCESSED/FAILED/DEAD_LETTERED)
- Durable lease acquisition, heartbeat, and expiration reclaim
- Stale generation invalidation
- Idempotency deduplication
"""

from __future__ import annotations

import threading
import time
import uuid
from pathlib import Path
from typing import Any

from .models import AgentTrigger, DeadLetterRecord, TriggerStatus, TriggerType
from .store import AgentRuntimeStore


class TriggerManager:
    """Manages triggers and durable worker leases."""

    def __init__(self, project_root: str | Path, store: AgentRuntimeStore | None = None) -> None:
        self.project_root = Path(project_root)
        self.store = store or AgentRuntimeStore(project_root)
        self._lock = threading.Lock()

    def create_trigger(
        self,
        channel_id: str,
        trigger_type: TriggerType | str,
        *,
        principal_id: str = "default",
        message_id: str | None = None,
        payload: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
        available_at: float | None = None,
        expires_at: float | None = None,
        max_attempts: int = 3,
        generation: int | None = None,
    ) -> AgentTrigger:
        """Create or return existing trigger if idempotency_key matches."""
        if idempotency_key:
            existing = self.store.find_trigger_by_idempotency_key(idempotency_key)
            if existing:
                return existing

        t_type = (
            trigger_type
            if isinstance(trigger_type, TriggerType)
            else TriggerType(str(trigger_type).upper())
        )
        current_gen = generation if generation is not None else self.store.get_generation()
        now = time.time()
        trigger = AgentTrigger(
            trigger_id=f"trig_{uuid.uuid4().hex[:16]}",
            channel_id=channel_id,
            trigger_type=t_type,
            principal_id=principal_id,
            message_id=message_id,
            payload=dict(payload or {}),
            idempotency_key=idempotency_key,
            created_at=now,
            available_at=available_at if available_at is not None else now,
            expires_at=expires_at,
            attempt=0,
            max_attempts=max_attempts,
            status=TriggerStatus.PENDING,
            generation=current_gen,
        )
        self.store.save_trigger(trigger)
        return trigger

    def claim_next_trigger(
        self,
        worker_id: str,
        *,
        ttl_seconds: float = 30.0,
        current_generation: int | None = None,
        channel_id: str | None = None,
    ) -> AgentTrigger | None:
        """Atomically claim the next eligible trigger."""
        from .store import file_lock

        lock_path = self.store.runtime_dir / "triggers.lock"
        with self._lock, file_lock(lock_path):
            gen = (
                current_generation
                if current_generation is not None
                else self.store.get_generation()
            )
            now = time.time()
            triggers = self.store.list_triggers(channel_id=channel_id)

            for trigger in triggers:
                if trigger.expires_at and trigger.expires_at <= now:
                    # Expired trigger
                    if trigger.status != TriggerStatus.DEAD_LETTERED:
                        trigger.status = TriggerStatus.DEAD_LETTERED
                        trigger.last_error = "trigger expired before processing"
                        self.store.save_trigger(trigger)
                    continue

                if trigger.available_at > now:
                    continue

                can_claim = False
                if trigger.status == TriggerStatus.PENDING:
                    can_claim = True
                elif trigger.status == TriggerStatus.CLAIMED:
                    # Expired lease or older generation
                    stale_lease = (
                        trigger.lease_expires_at is not None and trigger.lease_expires_at <= now
                    )
                    stale_gen = trigger.generation < gen
                    if stale_lease or stale_gen:
                        can_claim = True

                if can_claim:
                    if trigger.attempt >= trigger.max_attempts:
                        # Move to dead-letter
                        trigger.status = TriggerStatus.DEAD_LETTERED
                        trigger.last_error = f"max attempts ({trigger.max_attempts}) exhausted"
                        self.store.save_trigger(trigger)
                        self.store.save_dead_letter(
                            DeadLetterRecord(
                                dead_letter_id=f"dl_{uuid.uuid4().hex[:12]}",
                                entity_type="trigger",
                                entity_id=trigger.trigger_id,
                                channel_id=trigger.channel_id,
                                payload=trigger.payload,
                                reason="max_attempts_exhausted",
                                error_detail=trigger.last_error or "",
                                original_idempotency_key=trigger.idempotency_key,
                                attempts=trigger.attempt,
                                created_at=now,
                            )
                        )
                        continue

                    trigger.status = TriggerStatus.CLAIMED
                    trigger.claimed_by = worker_id
                    trigger.lease_id = uuid.uuid4().hex
                    trigger.lease_expires_at = now + ttl_seconds
                    trigger.generation = gen
                    trigger.attempt += 1
                    self.store.save_trigger(trigger)
                    return trigger

            return None

    def heartbeat_lease(
        self,
        trigger_id: str,
        lease_id: str,
        ttl_seconds: float = 30.0,
        worker_generation: int | None = None,
    ) -> bool:
        """Extend lease expiration for active worker."""
        current_gen = self.store.get_generation()
        if worker_generation is not None and worker_generation < current_gen:
            return False

        trigger = self.store.get_trigger(trigger_id)
        if not trigger or trigger.status != TriggerStatus.CLAIMED:
            return False
        if trigger.generation < current_gen:
            return False
        if trigger.lease_id != lease_id:
            return False

        trigger.lease_expires_at = time.time() + ttl_seconds
        self.store.save_trigger(trigger)
        return True

    def ack_trigger(
        self,
        trigger_id: str,
        lease_id: str,
        *,
        status: TriggerStatus = TriggerStatus.PROCESSED,
        worker_generation: int | None = None,
    ) -> bool:
        """Acknowledge completion of trigger processing."""
        current_gen = self.store.get_generation()
        if worker_generation is not None and worker_generation < current_gen:
            return False

        trigger = self.store.get_trigger(trigger_id)
        if not trigger:
            return False
        if trigger.generation < current_gen:
            return False
        if trigger.lease_id != lease_id:
            return False

        trigger.status = status
        trigger.lease_id = None
        trigger.lease_expires_at = None
        trigger.claimed_by = None
        self.store.save_trigger(trigger)
        return True

    def fail_trigger(
        self,
        trigger_id: str,
        lease_id: str,
        error: str,
        worker_generation: int | None = None,
    ) -> AgentTrigger:
        """Record trigger failure, retry or dead-letter."""
        current_gen = self.store.get_generation()
        if worker_generation is not None and worker_generation < current_gen:
            raise PermissionError(
                f"STALE_GENERATION_COMMIT: DENIED (worker_gen={worker_generation}, current_gen={current_gen})"
            )

        trigger = self.store.get_trigger(trigger_id)
        if trigger and trigger.generation < current_gen:
            raise PermissionError(
                f"STALE_GENERATION_COMMIT: DENIED (trigger_gen={trigger.generation}, current_gen={current_gen})"
            )

        if not trigger or trigger.lease_id != lease_id:
            raise KeyError(f"Trigger {trigger_id} not actively claimed with lease {lease_id}")

        trigger.last_error = error
        trigger.lease_id = None
        trigger.lease_expires_at = None
        trigger.claimed_by = None

        if trigger.attempt >= trigger.max_attempts:
            trigger.status = TriggerStatus.DEAD_LETTERED
            self.store.save_trigger(trigger)
            self.store.save_dead_letter(
                DeadLetterRecord(
                    dead_letter_id=f"dl_{uuid.uuid4().hex[:12]}",
                    entity_type="trigger",
                    entity_id=trigger.trigger_id,
                    channel_id=trigger.channel_id,
                    payload=trigger.payload,
                    reason="trigger_processing_failed",
                    error_detail=error,
                    original_idempotency_key=trigger.idempotency_key,
                    attempts=trigger.attempt,
                    created_at=time.time(),
                )
            )
        else:
            trigger.status = TriggerStatus.PENDING
            self.store.save_trigger(trigger)

        return trigger

    def reclaim_stale_leases(self, current_generation: int) -> int:
        """Reclaim all CLAIMED triggers whose lease expired or belong to an older generation."""
        now = time.time()
        triggers = self.store.list_triggers(status=TriggerStatus.CLAIMED)
        reclaimed = 0

        for t in triggers:
            stale_lease = t.lease_expires_at is not None and t.lease_expires_at <= now
            stale_gen = t.generation < current_generation
            if stale_lease or stale_gen:
                t.status = TriggerStatus.PENDING
                t.claimed_by = None
                t.lease_id = None
                t.lease_expires_at = None
                self.store.save_trigger(t)
                reclaimed += 1

        return reclaimed
