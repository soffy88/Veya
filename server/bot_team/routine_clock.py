import asyncio
import json
import logging
import os
import re
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from server.goal_run.routine_registry import RoutineRegistry
from server.goal_run.routine_dispatch import run_probe
from server.goal_run.models import RoutineSpec
from server.goal_run.routine_history import RoutineHistoryStore

_lg = logging.getLogger(__name__)

# NANO-W7: Daily brief routine template
DAILY_BRIEF_ROUTINE_TEMPLATE = {
    "routine_id": "daily_brief",
    "name": "晨间简报",
    "description": "Generate and deliver personalized morning feed",
    "enabled": False,  # Enabled per-user via VEYA_MORNING_FEED flag
    "schedule": "daily 08:00",
    "timezone": "Asia/Shanghai",
    "goal_template": "Generate daily brief for user {user_id}",
    "probe": {"kind": "always_pass"},
    "model": "veya1.2",
    "workspace_path": None,
    "bot_id": "system",
    "metadata": {
        "template": "daily_brief",
        "nano_w7": True,
    },
}


def create_daily_brief_routine(user_id: str, *, time: str = "08:00", timezone: str = "Asia/Shanghai") -> RoutineSpec:
    """Create a daily brief routine spec for a specific user.

    This is a template that the routine clock can use when VEYA_MORNING_FEED is enabled.
    The actual execution goes through the daily_brief omodul via MasterAgent.
    """
    return RoutineSpec(
        routine_id=f"daily_brief_{user_id}",
        name=f"晨间简报 ({user_id})",
        description="Generate and deliver personalized morning feed",
        enabled=True,
        schedule=f"daily {time}",
        timezone=timezone,
        goal_template=f"Generate daily brief for user {user_id}",
        probe={"kind": "always_pass"},
        model="veya1.2",
        workspace_path=None,
        bot_id="system",
        metadata={
            "template": "daily_brief",
            "nano_w7": True,
            "user_id": user_id,
        },
    )


async def _daily_brief_handoff_consumer(handoffs: list[dict]) -> None:
    """Handoff consumer for daily brief routines.

    Instead of running a full agent, this triggers the daily_brief omodul
    which assembles the feed and delivers via Web Push + channels.
    """
    if not handoffs:
        return

    try:
        from veya.omodul.daily_brief import daily_brief, DailyBriefConfig, DailyBriefInput
        from server.coordinator_master import master_coordinator
    except ImportError:
        _lg.warning("daily_brief omodul not available, skipping")
        return

    for handoff in handoffs:
        routine_id = handoff.get("routine_id", "")
        user_id = handoff.get("target", {}).get("user_id", "unknown")
        metadata = handoff.get("metadata", {})

        # Extract user_id from routine_id if not in target
        if user_id == "unknown" and routine_id.startswith("daily_brief_"):
            user_id = routine_id[len("daily_brief_"):]

        # Fetch data sections (would integrate with actual data sources)
        # For now, use empty data - in production this would query routines, goals, goalruns, memory, RSS
        input_data = DailyBriefInput(
            routines=[],
            goals=[],
            goalruns=[],
            memory_highlights=[],
            rss_items=[],
        )

        config = DailyBriefConfig(
            user_id=user_id,
            timezone=metadata.get("timezone", "Asia/Shanghai"),
            brief_time=metadata.get("brief_time", "08:00"),
        )

        # Run daily_brief omodul
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir)
            result = await daily_brief(config, input_data, output_dir)
            if result.get("status") == "completed":
                _lg.info(f"Daily brief generated for {user_id}: {result.get('fingerprint')}")
            else:
                _lg.error(f"Daily brief failed for {user_id}: {result.get('error')}")

def _get_tz(tz_str: str) -> ZoneInfo:
    try:
        return ZoneInfo(tz_str)
    except Exception:
        return ZoneInfo("Asia/Shanghai")

def compute_due(spec, *, now: datetime, last_fired_utc: datetime) -> str | None:
    """Return the next scheduled ISO string if it is <= now, else None."""
    if not spec.schedule:
        return None

    schedule = spec.schedule.strip()
    tz = _get_tz(spec.timezone or "Asia/Shanghai")

    # 1. Interval
    m = re.match(r'^every (\d+)(m|h|d)$', schedule)
    if m:
        val = int(m.group(1))
        unit = m.group(2)
        if unit == 'm':
            interval_s = val * 60
        elif unit == 'h':
            interval_s = val * 3600
        else:
            interval_s = val * 86400

        next_due = last_fired_utc + timedelta(seconds=interval_s)
        if now >= next_due:
            # Catch up: just return the most recent missed occurrence
            # Actually, the most recent missed occurrence is `now - ((now - last_fired_utc) % interval_s)`
            # Or we can just return next_due and when it updates last_fired_utc to next_due, it will catch up gradually.
            # But the spec says "emit AT MOST ONE missed occurrence per routine after downtime".
            # If we return next_due, the next tick will evaluate again based on updated last_fired_utc.
            # To strictly emit AT MOST ONE and then resume normal schedule, we should fast-forward last_fired_utc.
            # But compute_due just returns the due time.
            # If we return `now` formatted? Or `next_due`?
            # Let's fast forward to the MOST RECENT past occurrence.
            delta_s = (now - last_fired_utc).total_seconds()
            periods = int(delta_s // interval_s)
            recent_due = last_fired_utc + timedelta(seconds=periods * interval_s)
            return recent_due.isoformat()
        return None

    # 2. Daily
    m = re.match(r'^daily (\d{1,2}):(\d{2})$', schedule)
    if m:
        h, min_ = int(m.group(1)), int(m.group(2))

        # Start searching from last_fired_utc + 1 minute
        curr = last_fired_utc + timedelta(minutes=1)
        # We need to find the next time the local time matches h:min_
        # Loop day by day
        for _ in range(365):
            curr_local = curr.astimezone(tz)
            candidate_local = curr_local.replace(hour=h, minute=min_, second=0, microsecond=0)
            candidate_utc = candidate_local.astimezone(UTC)
            if candidate_utc <= last_fired_utc:
                # Add a day if it's in the past relative to last_fired
                candidate_local += timedelta(days=1)
                candidate_utc = candidate_local.astimezone(UTC)

            if last_fired_utc < candidate_utc <= now:
                # Fast forward to most recent past occurrence
                # For daily, just check if the most recent one is <= now
                most_recent_utc = candidate_utc
                while True:
                    next_local = most_recent_utc.astimezone(tz) + timedelta(days=1)
                    next_utc = next_local.astimezone(UTC)
                    if next_utc <= now:
                        most_recent_utc = next_utc
                    else:
                        break
                return most_recent_utc.isoformat()
            if candidate_utc > now:
                return None
            curr += timedelta(days=1)

        return None

    # 3. Cron
    parts = schedule.split()
    if len(parts) == 5:
        # minimal cron parser
        def match_field(val, expr):
            if expr == '*': return True
            if ',' in expr:
                return any(match_field(val, e) for e in expr.split(','))
            if expr.startswith('*/'):
                step = int(expr[2:])
                return val % step == 0
            if '-' in expr:
                start, end = map(int, expr.split('-'))
                return start <= val <= end
            return val == int(expr)

        curr = last_fired_utc + timedelta(minutes=1)
        curr = curr.replace(second=0, microsecond=0)

        # We find the *first* occurrence after last_fired_utc
        first_occurrence = None
        for _ in range(366 * 24 * 60): # up to a year
            if curr > now:
                break
            local = curr.astimezone(tz)
            if (match_field(local.minute, parts[0]) and
                match_field(local.hour, parts[1]) and
                match_field(local.day, parts[2]) and
                match_field(local.month, parts[3])):

                # Handle dow: 0=Sun, 6=Sat, 7=Sun
                # weekday() is 0=Mon, 6=Sun
                dow = (local.weekday() + 1) % 7
                if parts[4] == '*' or match_field(dow, parts[4]) or (parts[4] == '7' and dow == 0):
                    first_occurrence = curr
                    break
            curr += timedelta(minutes=1)

        if first_occurrence and first_occurrence <= now:
            # Catch up: fast-forward to the most recent occurrence <= now
            recent_occurrence = first_occurrence
            curr = now.replace(second=0, microsecond=0)
            while curr >= first_occurrence:
                local = curr.astimezone(tz)
                if (match_field(local.minute, parts[0]) and
                    match_field(local.hour, parts[1]) and
                    match_field(local.day, parts[2]) and
                    match_field(local.month, parts[3])):
                    dow = (local.weekday() + 1) % 7
                    if parts[4] == '*' or match_field(dow, parts[4]) or (parts[4] == '7' and dow == 0):
                        recent_occurrence = curr
                        break
                curr -= timedelta(minutes=1)
            return recent_occurrence.isoformat()

        return None

    return None

async def _default_handoff_consumer(handoffs: list[dict], *, bot_registry=None) -> None:
    if not handoffs:
        return

    from server.coordinator_master import master_coordinator
    from server.models.execution import (
        CanonicalExecutionRequest,
        ExecutionConstraints,
        ExecutionMode,
    )
    from server.notification_center import global_notifier

    for handoff in handoffs:
        bot_id = handoff.get("bot_id", "default")
        routine_id = handoff.get("routine_id", "")
        trigger_id = handoff.get("trigger_id", "")
        target = handoff.get("target", {})
        goal_text = target.get("goal_template", f"Run routine {routine_id}")

        req = CanonicalExecutionRequest(
            source="ROUTINE",
            mode=ExecutionMode.STRUCTURED_CONSTRAINED,
            objective=goal_text,
            project_root=".",
            constraints=ExecutionConstraints(
                metadata={
                    "bot_id": bot_id,
                    "routine_id": routine_id,
                    "trigger_id": trigger_id,
                }
            )
        )

        try:
            result = await master_coordinator.execute_structured(req)
            first_line = str(result).split('\n')[0] if result else "Success"
            global_notifier.push(
                type="SUCCESS",
                title="Routine finished",
                content=first_line,
                payload={"routine_id": routine_id},
                user_id="system"
            )
        except Exception as e:
            _lg.exception(f"Handoff execution failed for routine {routine_id}")
            global_notifier.push(
                type="ERROR",
                title="Routine failed",
                content=str(e),
                payload={"routine_id": routine_id},
                user_id="system"
            )

class RoutineClock:
    def __init__(self, registry: RoutineRegistry, *, clock_path=None, dispatch_fn=None):
        self.registry = registry
        if clock_path is None:
            clock_path = os.environ.get("VEYA_ROUTINE_CLOCK_PATH", os.path.expanduser("~/.veya/routines_clock.json"))
        self.clock_path = Path(clock_path)
        self.dispatch_fn = dispatch_fn or _default_handoff_consumer
        self.last_fired: dict[str, str] = {}
        self._load_state()

    def _load_state(self):
        if self.clock_path.exists():
            try:
                self.last_fired = json.loads(self.clock_path.read_text())
            except Exception:
                _lg.exception("Failed to load routine clock state")
                self.last_fired = {}

    def _save_state(self):
        try:
            self.clock_path.parent.mkdir(parents=True, exist_ok=True)
            self.clock_path.write_text(json.dumps(self.last_fired))
        except Exception:
            _lg.exception("Failed to save routine clock state")

    async def tick(self) -> list[dict]:
        now = datetime.now(UTC)
        handoffs = []

        # Get all specs for finding workspace_path later
        specs = self.registry.list(include_disabled=True)

        # P3-A: We might need to iterate over all routines.
        # But RoutineRegistry typically belongs to a single bot_id.
        # If the bot_id is scoped in RoutineRegistry, this is fine.

        for routine_id, spec in [(s.routine_id, s) for s in specs]:
            if not spec.enabled:
                continue

            # last_fired initialization
            if routine_id not in self.last_fired:
                self.last_fired[routine_id] = now.isoformat()
                self._save_state()
                continue

            last_fired_utc = datetime.fromisoformat(self.last_fired[routine_id])

            due_iso = compute_due(spec, now=now, last_fired_utc=last_fired_utc)
            if due_iso:
                trigger_id = f"{routine_id}:{due_iso}"

                # ORCA-W4: Run probe before handoff (mirrors dispatch_trigger logic)
                probe_result = run_probe(spec.probe, spec.workspace_path or None)
                moment = time.time()
                run_record = {
                    "trigger_id": trigger_id,
                    "trigger_identity": trigger_id,
                    "dispatched_at": moment,
                    "probe": probe_result,
                }

                # Record run history in routine state
                try:
                    if not probe_result["success"]:
                        # Probe failed - record with probe_error status
                        run_record["status"] = "probe_error"
                    elif probe_result["kind"] == "git_changes" and not probe_result["data"].get("has_changes", False):
                        # No changes - record as skipped
                        run_record["status"] = "skipped"
                    else:
                        # Probe passed - record as dispatched
                        run_record["status"] = "dispatched"
                    RoutineHistoryStore(bot_id=spec.bot_id).add_record(routine_id, run_record)
                except Exception:
                    # Non-fatal: continue with dispatch
                    pass

                # Dispatch only if probe didn't skip or error
                if not probe_result["success"]:
                    # Probe failed - skip dispatch
                    self.last_fired[routine_id] = now.isoformat()
                    self._save_state()
                    continue
                elif probe_result["kind"] == "git_changes" and not probe_result["data"].get("has_changes", False):
                    # No changes detected - skip dispatch
                    self.last_fired[routine_id] = now.isoformat()
                    self._save_state()
                    continue
                else:
                    # Probe passed (has_changes or other probe kinds), dispatch
                    handoffs.append({
                        "routine_id": routine_id,
                        "trigger_topic": "schedule.trigger",
                        "trigger_id": trigger_id,
                        "target": {"goal_template": spec.goal_template},
                        "bot_id": spec.bot_id,
                        "model": spec.model,
                        "workspace_path": spec.workspace_path,
                    })
                    self.last_fired[routine_id] = now.isoformat()
                    self._save_state()

        if handoffs:
            # Split handoffs: daily_brief goes to special consumer, others to default
            daily_brief_handoffs = [h for h in handoffs if h.get("metadata", {}).get("template") == "daily_brief"]
            regular_handoffs = [h for h in handoffs if h.get("metadata", {}).get("template") != "daily_brief"]

            if daily_brief_handoffs:
                try:
                    await _daily_brief_handoff_consumer(daily_brief_handoffs)
                except Exception:
                    _lg.exception("Failed to dispatch daily brief routines")

            if regular_handoffs:
                try:
                    await self.dispatch_fn(regular_handoffs)
                except Exception:
                    _lg.exception("Failed to dispatch scheduled routines")

        return handoffs

    async def run_forever(self, interval_s=30):
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                break
            except Exception:
                _lg.exception("Error in routine clock tick")
            await asyncio.sleep(interval_s)
