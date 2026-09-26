"""Agent Scheduler for Veya Agent Runtime V1.

Produces AgentTriggers on schedule without directly executing missions or tools.
Supports:
- ONCE, INTERVAL, and CRON schedules
- Missed schedule policies: FIRE_ONCE (default), SKIP, CATCH_UP_LIMITED
- Deterministic idempotency keys for every scheduled trigger
"""

from __future__ import annotations

import time
import uuid
from pathlib import Path
from typing import Any

from .models import (
    AgentSchedule,
    AgentTrigger,
    MissedSchedulePolicy,
    ScheduleType,
    TriggerType,
)
from .store import AgentRuntimeStore
from .trigger import TriggerManager


def _parse_cron_field(val: str, min_val: int, max_val: int) -> set[int]:
    """Simple parser for standard cron field."""
    val = val.strip()
    if val == "*":
        return set(range(min_val, max_val + 1))
    if val.startswith("*/"):
        step = int(val[2:])
        return set(range(min_val, max_val + 1, step))
    res = set()
    for part in val.split(","):
        if "-" in part:
            lo, hi = part.split("-", 1)
            res.update(range(int(lo), int(hi) + 1))
        else:
            res.add(int(part))
    return res


def compute_next_cron(expression: str, after_timestamp: float) -> float:
    """Compute the next epoch second matching the 5-field cron expression."""
    fields = expression.split()
    if len(fields) != 5:
        # Fallback to interval if not 5 fields
        try:
            return after_timestamp + float(expression)
        except ValueError:
            return after_timestamp + 60.0

    min_set = _parse_cron_field(fields[0], 0, 59)
    hour_set = _parse_cron_field(fields[1], 0, 23)
    dom_set = _parse_cron_field(fields[2], 1, 31)
    mon_set = _parse_cron_field(fields[3], 1, 12)
    dow_set = _parse_cron_field(fields[4], 0, 6)

    # Search forward minute by minute up to 366 days
    current = int(after_timestamp) - (int(after_timestamp) % 60) + 60
    max_search = current + (366 * 86400)
    while current < max_search:
        tm = time.gmtime(current)
        # tm_wday: 0=Monday, 6=Sunday; cron: 0=Sunday, 6=Saturday or 0=Sun..6=Sat
        cron_wday = (tm.tm_wday + 1) % 7
        if (
            tm.tm_min in min_set
            and tm.tm_hour in hour_set
            and tm.tm_mday in dom_set
            and tm.tm_mon in mon_set
            and cron_wday in dow_set
        ):
            return float(current)
        current += 60

    return after_timestamp + 3600.0


class AgentScheduler:
    """Evaluates schedules and generates scheduled AgentTriggers."""

    def __init__(
        self,
        project_root: str | Path,
        store: AgentRuntimeStore | None = None,
        trigger_manager: TriggerManager | None = None,
    ) -> None:
        self.project_root = Path(project_root)
        self.store = store or AgentRuntimeStore(project_root)
        self.triggers = trigger_manager or TriggerManager(project_root, self.store)

    def calculate_next_fire(self, schedule: AgentSchedule, after_time: float) -> float:
        """Calculate next fire epoch based on schedule type."""
        if schedule.schedule_type == ScheduleType.INTERVAL:
            try:
                interval_s = float(schedule.expression)
            except ValueError:
                interval_s = 60.0
            return after_time + interval_s
        elif schedule.schedule_type == ScheduleType.ONCE:
            try:
                target = float(schedule.expression)
                if target < 1e9:
                    # Relative seconds
                    return after_time + target
                return target
            except ValueError:
                return after_time + 60.0
        elif schedule.schedule_type == ScheduleType.CRON:
            return compute_next_cron(schedule.expression, after_time)
        return after_time + 60.0

    def create_schedule(
        self,
        channel_id: str,
        schedule_type: ScheduleType | str,
        expression: str,
        *,
        principal_id: str = "default",
        payload: dict[str, Any] | None = None,
        missed_policy: MissedSchedulePolicy | str = MissedSchedulePolicy.FIRE_ONCE,
        enabled: bool = True,
    ) -> AgentSchedule:
        """Create and persist a new schedule."""
        st = (
            schedule_type
            if isinstance(schedule_type, ScheduleType)
            else ScheduleType(str(schedule_type).upper())
        )
        mp = (
            missed_policy
            if isinstance(missed_policy, MissedSchedulePolicy)
            else MissedSchedulePolicy(str(missed_policy).upper())
        )
        now = time.time()
        sched = AgentSchedule(
            schedule_id=f"sched_{uuid.uuid4().hex[:12]}",
            channel_id=channel_id,
            principal_id=principal_id,
            schedule_type=st,
            expression=expression,
            payload=dict(payload or {}),
            enabled=enabled,
            missed_policy=mp,
            next_fire_at=0.0,
            created_at=now,
            updated_at=now,
        )
        sched.next_fire_at = self.calculate_next_fire(sched, now)
        self.store.save_schedule(sched)
        return sched

    def set_enabled(self, schedule_id: str, enabled: bool) -> bool:
        sched = self.store.get_schedule(schedule_id)
        if not sched:
            return False
        sched.enabled = enabled
        sched.updated_at = time.time()
        if enabled and sched.next_fire_at <= time.time():
            sched.next_fire_at = self.calculate_next_fire(sched, time.time())
        self.store.save_schedule(sched)
        return True

    def tick(self, now: float | None = None) -> list[AgentTrigger]:
        """Scan schedules and generate triggers for due items."""
        now_ts = now if now is not None else time.time()
        schedules = self.store.list_schedules()
        generated_triggers: list[AgentTrigger] = []

        for sched in schedules:
            if not sched.enabled:
                continue
            if sched.next_fire_at > now_ts:
                continue

            # Determine firing count based on missed policy
            is_missed = (now_ts - sched.next_fire_at) > 120.0  # More than 2 min behind
            fire_count = 1
            if is_missed:
                if sched.missed_policy == MissedSchedulePolicy.SKIP:
                    fire_count = 0
                elif sched.missed_policy == MissedSchedulePolicy.CATCH_UP_LIMITED:
                    fire_count = min(3, max(1, int((now_ts - sched.next_fire_at) / 60)))
                else:
                    # FIRE_ONCE
                    fire_count = 1

            for i in range(fire_count):
                fire_ts = int(sched.next_fire_at) + i
                idempotency_key = f"sched_{sched.schedule_id}_{fire_ts}"
                trigger = self.triggers.create_trigger(
                    channel_id=sched.channel_id,
                    trigger_type=TriggerType.SCHEDULE,
                    principal_id=sched.principal_id,
                    payload={
                        "schedule_id": sched.schedule_id,
                        "schedule_type": str(sched.schedule_type),
                        "fire_timestamp": fire_ts,
                        **sched.payload,
                    },
                    idempotency_key=idempotency_key,
                )
                generated_triggers.append(trigger)
                sched.last_trigger_id = trigger.trigger_id

            sched.last_fire_at = now_ts
            if sched.schedule_type == ScheduleType.ONCE:
                sched.enabled = False
            else:
                sched.next_fire_at = self.calculate_next_fire(sched, now_ts)

            sched.updated_at = now_ts
            self.store.save_schedule(sched)

        return generated_triggers
