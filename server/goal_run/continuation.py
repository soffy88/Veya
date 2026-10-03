"""P0-04 Continuation Trigger — durable trigger → claim → GoalRun admission.

The scheduler NEVER executes tasks. It only:
  1. Detects a due trigger
  2. Durably claims it (preventing duplicate execution)
  3. Advances the schedule durably
  4. Admits a continuation into the existing GoalRun authority

Trigger policies:
  COALESCE:       missed ticks collapse into one pending trigger
  SKIP_MISSED:    missed ticks are dropped
  CATCH_UP_BOUNDED: missed ticks fire up to a bounded limit
  ONE_SHOT:       trigger fires once and is done
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any


class TriggerType(StrEnum):
    HEARTBEAT = "HEARTBEAT"
    SCHEDULE = "SCHEDULE"
    EVENT = "EVENT"
    MANUAL_RESUME = "MANUAL_RESUME"
    DEPENDENCY_READY = "DEPENDENCY_READY"
    RETRY = "RETRY"


class TriggerPolicy(StrEnum):
    COALESCE = "COALESCE"
    SKIP_MISSED = "SKIP_MISSED"
    CATCH_UP_BOUNDED = "CATCH_UP_BOUNDED"
    ONE_SHOT = "ONE_SHOT"


class TriggerStatus(StrEnum):
    PENDING = "PENDING"
    CLAIMED = "CLAIMED"
    FIRED = "FIRED"
    CANCELLED = "CANCELLED"


@dataclass(frozen=True)
class ContinuationTrigger:
    trigger_id: str
    goal_run_id: str
    type: TriggerType
    status: TriggerStatus = TriggerStatus.PENDING
    schedule: dict[str, Any] | None = None
    event_condition: dict[str, Any] | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    next_due_at: float | None = None
    last_claim_id: str | None = None
    policy: TriggerPolicy = TriggerPolicy.COALESCE
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["type"] = self.type.value
        data["status"] = self.status.value
        data["policy"] = self.policy.value
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ContinuationTrigger:
        return cls(
            trigger_id=str(data["trigger_id"]),
            goal_run_id=str(data["goal_run_id"]),
            type=TriggerType(data.get("type", "MANUAL_RESUME")),
            status=TriggerStatus(data.get("status", "PENDING")),
            schedule=data.get("schedule"),
            event_condition=data.get("event_condition"),
            payload=dict(data.get("payload") or {}),
            next_due_at=data.get("next_due_at"),
            last_claim_id=data.get("last_claim_id"),
            policy=TriggerPolicy(data.get("policy", "COALESCE")),
            created_at=float(data.get("created_at", time.time())),
            updated_at=float(data.get("updated_at", time.time())),
        )


class ContinuationTriggerStore:
    """Durable JSON-file store for continuation triggers."""

    def __init__(self, path: str | Path | None = None):
        if path is None:
            path = Path.home() / ".veya" / "continuation_triggers.json"
        self.path = Path(path).expanduser()
        self._lock = threading.RLock()

    def _read_all(self) -> dict[str, dict[str, Any]]:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (json.JSONDecodeError, OSError):
            return {}

    def _write_all(self, triggers: dict[str, dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(triggers, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def save(self, trigger: ContinuationTrigger) -> None:
        with self._lock:
            all_triggers = self._read_all()
            all_triggers[trigger.trigger_id] = trigger.to_dict()
            self._write_all(all_triggers)

    def get(self, trigger_id: str) -> ContinuationTrigger | None:
        with self._lock:
            data = self._read_all().get(trigger_id)
            return ContinuationTrigger.from_dict(data) if data else None

    def list_for_goal(self, goal_run_id: str) -> list[ContinuationTrigger]:
        with self._lock:
            return [
                ContinuationTrigger.from_dict(t)
                for t in self._read_all().values()
                if t.get("goal_run_id") == goal_run_id
            ]

    def list_due(self, *, now: float | None = None) -> list[ContinuationTrigger]:
        now = now or time.time()
        with self._lock:
            result = []
            for t in self._read_all().values():
                if t.get("status") != "PENDING":
                    continue
                due = t.get("next_due_at")
                if due is not None and float(due) <= now:
                    result.append(ContinuationTrigger.from_dict(t))
            return result


class ContinuationTriggerManager:
    """Manages the trigger → claim → admit pipeline.

    The manager NEVER executes tasks. It only:
      1. Claims a due trigger durably
      2. Advances the schedule durably
      3. Returns the claim for the caller to admit into GoalRun
    """

    def __init__(self, store: ContinuationTriggerStore | None = None):
        self.store = store or ContinuationTriggerStore()
        self._claim_lock = threading.RLock()

    def create_trigger(
        self,
        *,
        goal_run_id: str,
        trigger_type: TriggerType,
        schedule: dict[str, Any] | None = None,
        event_condition: dict[str, Any] | None = None,
        payload: dict[str, Any] | None = None,
        next_due_at: float | None = None,
        policy: TriggerPolicy = TriggerPolicy.COALESCE,
    ) -> ContinuationTrigger:
        trigger = ContinuationTrigger(
            trigger_id=str(uuid.uuid4()),
            goal_run_id=goal_run_id,
            type=trigger_type,
            schedule=schedule,
            event_condition=event_condition,
            payload=dict(payload or {}),
            next_due_at=next_due_at,
            policy=policy,
        )
        self.store.save(trigger)
        return trigger

    def claim_next(
        self,
        *,
        goal_run_id: str,
        now: float | None = None,
    ) -> ContinuationTrigger | None:
        """Durably claim the next due trigger for a goal.

        Returns the claimed trigger, or None if no trigger is due.
        The caller is responsible for admitting the continuation into GoalRun.
        """
        now = now or time.time()
        with self._claim_lock:
            due = self.store.list_due(now=now)
            for trigger in due:
                if trigger.goal_run_id != goal_run_id:
                    continue
                claim_id = str(uuid.uuid4())
                claimed = ContinuationTrigger.from_dict(
                    {
                        **trigger.to_dict(),
                        "status": TriggerStatus.CLAIMED.value,
                        "last_claim_id": claim_id,
                        "updated_at": now,
                    }
                )
                self.store.save(claimed)
                return claimed
            return None

    def fire_claim(self, claim_id: str) -> ContinuationTrigger | None:
        """Mark a claim as fired and advance the schedule."""
        with self._claim_lock:
            for trigger in self.store.list_for_goal(""):
                if trigger.last_claim_id == claim_id:
                    now = time.time()
                    next_due = self._advance_schedule(trigger, now)
                    fired = ContinuationTrigger.from_dict(
                        {
                            **trigger.to_dict(),
                            "status": (
                                TriggerStatus.FIRED.value
                                if next_due is None
                                else TriggerStatus.PENDING.value
                            ),
                            "next_due_at": next_due,
                            "updated_at": now,
                        }
                    )
                    self.store.save(fired)
                    return fired
            return None

    def _advance_schedule(self, trigger: ContinuationTrigger, now: float) -> float | None:
        """Compute the next due time based on the trigger's schedule and policy."""
        if trigger.policy is TriggerPolicy.ONE_SHOT:
            return None
        schedule = trigger.schedule or {}
        interval = float(schedule.get("interval_seconds", 0))
        if interval <= 0:
            return None
        if trigger.policy is TriggerPolicy.SKIP_MISSED:
            return now + interval
        if trigger.policy is TriggerPolicy.CATCH_UP_BOUNDED:
            max_catchup = int(schedule.get("max_catch_up", 3))
            missed = int((now - (trigger.next_due_at or now)) / interval)
            if missed > max_catchup:
                return now + interval
            return (trigger.next_due_at or now) + interval
        return (trigger.next_due_at or now) + interval

    def cancel_for_goal(self, goal_run_id: str) -> int:
        """Cancel all pending triggers for a goal. Returns count cancelled."""
        with self._claim_lock:
            count = 0
            for trigger in self.store.list_for_goal(goal_run_id):
                if trigger.status is TriggerStatus.PENDING:
                    cancelled = ContinuationTrigger.from_dict(
                        {
                            **trigger.to_dict(),
                            "status": TriggerStatus.CANCELLED.value,
                            "updated_at": time.time(),
                        }
                    )
                    self.store.save(cancelled)
                    count += 1
            return count
