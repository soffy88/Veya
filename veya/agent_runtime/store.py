"""Durable Store for Veya Agent Runtime V1.

Provides atomic, crash-resilient persistence for:
- Monotonically increasing runtime generation
- Agent triggers and leases
- Agent schedules
- Event subscriptions
- Notification outbox deliveries
- Dead letter records
- Routing audit records
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import uuid
from collections.abc import Generator
from pathlib import Path
from typing import Any

from .models import (
    AgentSchedule,
    AgentTrigger,
    DeadLetterRecord,
    DeliveryStatus,
    EventSubscription,
    NotificationDelivery,
    TriggerStatus,
)


class StoreCorruptionError(RuntimeError):
    """Raised when durable runtime store data is corrupted or torn."""


@contextlib.contextmanager
def file_lock(lock_path: Path) -> Generator[None, None, None]:
    """Cross-process and cross-thread advisory file lock."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


class AgentRuntimeStore:
    """Atomic file-based durable store under `.veya/runtime/`."""

    def __init__(self, project_root: str | Path) -> None:
        self.project_root = Path(project_root)
        self.runtime_dir = self.project_root / ".veya" / "runtime"
        self.runtime_dir.mkdir(parents=True, exist_ok=True)

    def _atomic_write_json(self, path: Path, data: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(f".tmp.{uuid.uuid4().hex}")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        tmp.replace(path)

    def _atomic_write_jsonl(self, path: Path, items: list[dict[str, Any]]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(f".tmp.{uuid.uuid4().hex}")
        with tmp.open("w", encoding="utf-8") as f:
            for item in items:
                f.write(json.dumps(item, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())
        tmp.replace(path)

    def _read_jsonl(self, path: Path) -> list[dict[str, Any]]:
        if not path.is_file():
            return []
        items: list[dict[str, Any]] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                items.append(json.loads(line))
            except Exception as exc:
                raise StoreCorruptionError(
                    f"Corrupted record detected in {path.name}: {line[:50]}"
                ) from exc
        return items

    # --- Runtime Generation ---

    @property
    def generation_file(self) -> Path:
        return self.runtime_dir / "generation.json"

    def get_generation(self) -> int:
        if not self.generation_file.is_file():
            return 1
        try:
            data = json.loads(self.generation_file.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or "runtime_generation" not in data:
                raise StoreCorruptionError(
                    f"Corrupted generation structure in {self.generation_file}"
                )
            return int(data["runtime_generation"])
        except Exception as exc:
            if isinstance(exc, StoreCorruptionError):
                raise
            raise StoreCorruptionError(f"Corrupted generation file: {exc}") from exc

    def bump_generation(self) -> int:
        current = self.get_generation()
        nxt = current + 1
        self._atomic_write_json(self.generation_file, {"runtime_generation": nxt})
        return nxt

    # --- Triggers ---

    @property
    def triggers_file(self) -> Path:
        return self.runtime_dir / "triggers.jsonl"

    def save_trigger(self, trigger: AgentTrigger) -> None:
        items = self._read_jsonl(self.triggers_file)
        updated = False
        new_items = []
        for d in items:
            if d.get("trigger_id") == trigger.trigger_id:
                new_items.append(trigger.to_dict())
                updated = True
            else:
                new_items.append(d)
        if not updated:
            new_items.append(trigger.to_dict())
        self._atomic_write_jsonl(self.triggers_file, new_items)

    def get_trigger(self, trigger_id: str) -> AgentTrigger | None:
        for d in self._read_jsonl(self.triggers_file):
            if d.get("trigger_id") == trigger_id:
                return AgentTrigger.from_dict(d)
        return None

    def find_trigger_by_idempotency_key(self, idempotency_key: str) -> AgentTrigger | None:
        for d in self._read_jsonl(self.triggers_file):
            if d.get("idempotency_key") == idempotency_key:
                return AgentTrigger.from_dict(d)
        return None

    def list_triggers(
        self,
        channel_id: str | None = None,
        status: TriggerStatus | str | None = None,
    ) -> list[AgentTrigger]:
        items = self._read_jsonl(self.triggers_file)
        results: list[AgentTrigger] = []
        st_filter = str(status) if status is not None else None
        for d in items:
            t = AgentTrigger.from_dict(d)
            if channel_id and t.channel_id != channel_id:
                continue
            if st_filter and str(t.status) != st_filter:
                continue
            results.append(t)
        return results

    # --- Schedules ---

    @property
    def schedules_file(self) -> Path:
        return self.runtime_dir / "schedules.jsonl"

    def save_schedule(self, schedule: AgentSchedule) -> None:
        items = self._read_jsonl(self.schedules_file)
        updated = False
        new_items = []
        for d in items:
            if d.get("schedule_id") == schedule.schedule_id:
                new_items.append(schedule.to_dict())
                updated = True
            else:
                new_items.append(d)
        if not updated:
            new_items.append(schedule.to_dict())
        self._atomic_write_jsonl(self.schedules_file, new_items)

    def get_schedule(self, schedule_id: str) -> AgentSchedule | None:
        for d in self._read_jsonl(self.schedules_file):
            if d.get("schedule_id") == schedule_id:
                return AgentSchedule.from_dict(d)
        return None

    def list_schedules(self, channel_id: str | None = None) -> list[AgentSchedule]:
        items = self._read_jsonl(self.schedules_file)
        results: list[AgentSchedule] = []
        for d in items:
            s = AgentSchedule.from_dict(d)
            if channel_id and s.channel_id != channel_id:
                continue
            results.append(s)
        return results

    def delete_schedule(self, schedule_id: str) -> bool:
        items = self._read_jsonl(self.schedules_file)
        new_items = [d for d in items if d.get("schedule_id") != schedule_id]
        if len(new_items) != len(items):
            self._atomic_write_jsonl(self.schedules_file, new_items)
            return True
        return False

    # --- Subscriptions ---

    @property
    def subscriptions_file(self) -> Path:
        return self.runtime_dir / "subscriptions.jsonl"

    def save_subscription(self, sub: EventSubscription) -> None:
        items = self._read_jsonl(self.subscriptions_file)
        updated = False
        new_items = []
        for d in items:
            if d.get("subscription_id") == sub.subscription_id:
                new_items.append(sub.to_dict())
                updated = True
            else:
                new_items.append(d)
        if not updated:
            new_items.append(sub.to_dict())
        self._atomic_write_jsonl(self.subscriptions_file, new_items)

    def get_subscription(self, subscription_id: str) -> EventSubscription | None:
        for d in self._read_jsonl(self.subscriptions_file):
            if d.get("subscription_id") == subscription_id:
                return EventSubscription.from_dict(d)
        return None

    def list_subscriptions(self, channel_id: str | None = None) -> list[EventSubscription]:
        items = self._read_jsonl(self.subscriptions_file)
        results: list[EventSubscription] = []
        for d in items:
            sub = EventSubscription.from_dict(d)
            if channel_id and sub.channel_id != channel_id:
                continue
            results.append(sub)
        return results

    def delete_subscription(self, subscription_id: str) -> bool:
        items = self._read_jsonl(self.subscriptions_file)
        new_items = [d for d in items if d.get("subscription_id") != subscription_id]
        if len(new_items) != len(items):
            self._atomic_write_jsonl(self.subscriptions_file, new_items)
            return True
        return False

    # --- Notification Outbox ---

    @property
    def outbox_file(self) -> Path:
        return self.runtime_dir / "outbox.jsonl"

    def save_delivery(self, delivery: NotificationDelivery) -> None:
        items = self._read_jsonl(self.outbox_file)
        updated = False
        new_items = []
        for d in items:
            if d.get("delivery_id") == delivery.delivery_id:
                new_items.append(delivery.to_dict())
                updated = True
            else:
                new_items.append(d)
        if not updated:
            new_items.append(delivery.to_dict())
        self._atomic_write_jsonl(self.outbox_file, new_items)

    def get_delivery(self, delivery_id: str) -> NotificationDelivery | None:
        for d in self._read_jsonl(self.outbox_file):
            if d.get("delivery_id") == delivery_id:
                return NotificationDelivery.from_dict(d)
        return None

    def list_deliveries(
        self,
        status: DeliveryStatus | str | None = None,
    ) -> list[NotificationDelivery]:
        items = self._read_jsonl(self.outbox_file)
        results: list[NotificationDelivery] = []
        st_filter = str(status) if status is not None else None
        for d in items:
            deliv = NotificationDelivery.from_dict(d)
            if st_filter and str(deliv.status) != st_filter:
                continue
            results.append(deliv)
        return results

    # --- Dead Letter Queue ---

    @property
    def dead_letters_file(self) -> Path:
        return self.runtime_dir / "dead_letters.jsonl"

    def save_dead_letter(self, record: DeadLetterRecord) -> None:
        items = self._read_jsonl(self.dead_letters_file)
        updated = False
        new_items = []
        for d in items:
            if d.get("dead_letter_id") == record.dead_letter_id:
                new_items.append(record.to_dict())
                updated = True
            else:
                new_items.append(d)
        if not updated:
            new_items.append(record.to_dict())
        self._atomic_write_jsonl(self.dead_letters_file, new_items)

    def get_dead_letter(self, dead_letter_id: str) -> DeadLetterRecord | None:
        for d in self._read_jsonl(self.dead_letters_file):
            if d.get("dead_letter_id") == dead_letter_id:
                return DeadLetterRecord.from_dict(d)
        return None

    def list_dead_letters(
        self,
        entity_type: str | None = None,
    ) -> list[DeadLetterRecord]:
        items = self._read_jsonl(self.dead_letters_file)
        results: list[DeadLetterRecord] = []
        for d in items:
            rec = DeadLetterRecord.from_dict(d)
            if entity_type and rec.entity_type != entity_type:
                continue
            results.append(rec)
        return results

    # --- Routing Records ---

    @property
    def routing_file(self) -> Path:
        return self.runtime_dir / "routing_records.jsonl"

    def record_routing(self, record: dict[str, Any]) -> None:
        items = self._read_jsonl(self.routing_file)
        items.append(dict(record))
        self._atomic_write_jsonl(self.routing_file, items)

    def get_routing_records(
        self,
        trigger_id: str | None = None,
        mission_id: str | None = None,
    ) -> list[dict[str, Any]]:
        items = self._read_jsonl(self.routing_file)
        results: list[dict[str, Any]] = []
        for r in items:
            if trigger_id and r.get("trigger_id") != trigger_id:
                continue
            if mission_id and r.get("mission_id") != mission_id:
                continue
            results.append(r)
        return results
