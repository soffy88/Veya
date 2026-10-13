import asyncio
import json
import logging
import os
import re
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from server.goal_run.bot_identity import BotRegistry
from server.goal_run.routine_dispatch import run_probe
from server.goal_run.routine_history import RoutineHistoryStore
from server.goal_run.routine_registry import RoutineRegistry

_lg = logging.getLogger(__name__)

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

def _route_wake(
    *,
    bot_id: str,
    routine_id: str,
    trigger_id: str,
    summary: str,
    error: str = "",
    bot_registry=None,
) -> dict | None:
    """NANO-W10 item 2 — route a routine's result through the one wake intake.

    Returns ``None`` when ``VEYA_WAKE_ROUTER`` is off, which is the default, so
    the caller keeps the pre-existing notification byte for byte. With the flag
    on, push and channels fire **only** when the intake classifies the wake as
    ``reply_now`` — a routine that merely ticked is not worth bugging anyone.

    Args:
        bot_id: the bot that ran the routine.
        routine_id: the routine that fired.
        trigger_id: the existing ``routine_id:due_iso`` trigger identity; it is
            the wake's dedupe key, so one firing is one wake.
        summary: the first line of the run's result.
        error: the failure text, when the routine failed.
        bot_registry: injected catalog used to resolve the routine's owner.

    Returns:
        The intake result, or ``None`` when the router is off.
    """

    from server.nano_w10.wake_router import default_wake_router, wake_router_enabled

    if not wake_router_enabled():
        return None

    owner_ref = bot_id
    try:
        from server.goal_run.bot_identity import BotRegistry

        spec = (bot_registry or BotRegistry()).get(bot_id)
        owner_ref = str(getattr(spec, "owner_id", "") or bot_id)
    except Exception:
        _lg.debug("could not resolve the owner of %s; waking the bot id", bot_id, exc_info=True)

    return default_wake_router().ingest(
        owner_ref=owner_ref,
        source="routine_clock",
        summary=summary,
        signal_id=trigger_id,
        novel=bool(str(summary or "").strip()),
        urgent=bool(error),
        payload={"routine_id": routine_id, "bot_id": bot_id, "error": error},
    )


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

        # W12 supervisor path: if this routine is bound to a GoalRun via
        # a supervisor binding, run the supervisor handoff instead of
        # dispatching a bot turn.
        if await _try_supervisor_handoff(routine_id=routine_id, bot_id=str(bot_id)):
            continue

        # A bot-owned routine runs as a real bot turn: the same product path
        # the UI drives, so the bot's own answer lands in its conversation
        # (meta carries source/routine_id) instead of a raw GoalRun repr.
        bot_spec = _resolve_bot_for_routine(bot_id, bot_registry=bot_registry)
        if bot_spec is not None:
            await _run_bot_turn_handoff(
                bot_spec=bot_spec,
                goal_text=goal_text,
                routine_id=routine_id,
                trigger_id=trigger_id,
                bot_registry=bot_registry,
            )
            continue

        # Legacy unscoped path (no bot catalog entry): keep the pre-existing
        # notification byte for byte.
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
            try:
                from server.bot_team.conversations import conversation_store

                text_to_post = str(result or first_line or f"Routine {routine_id} executed").strip()
                conversation_store().append(
                    str(bot_id),
                    role="bot",
                    text=text_to_post,
                    meta={
                        "source": "routine",
                        "routine_id": str(routine_id),
                        "trigger_id": str(trigger_id),
                    },
                )
            except Exception:
                _lg.debug("could not append routine output to bot conversation", exc_info=True)
            routed = _route_wake(
                bot_id=str(bot_id),
                routine_id=str(routine_id),
                trigger_id=str(trigger_id),
                summary=first_line,
                bot_registry=bot_registry,
            )
            if routed is None:
                global_notifier.push(
                    type="SUCCESS",
                    title="Routine finished",
                    content=first_line,
                    payload={"routine_id": routine_id},
                    user_id="system"
                )
        except Exception as e:
            _lg.exception(f"Handoff execution failed for routine {routine_id}")
            routed = _route_wake(
                bot_id=str(bot_id),
                routine_id=str(routine_id),
                trigger_id=str(trigger_id),
                summary=str(e),
                error=str(e),
                bot_registry=bot_registry,
            )
            if routed is None:
                global_notifier.push(
                    type="ERROR",
                    title="Routine failed",
                    content=str(e),
                    payload={"routine_id": routine_id},
                    user_id="system"
                )


def _resolve_bot_for_routine(bot_id: str, *, bot_registry=None) -> "object | None":
    """Resolve the bot a routine runs as.

    Returns its :class:`BotSpec` when the catalog knows it (so the routine
    runs through the bot-turn pipeline), else ``None`` to fall back to the
    legacy unscoped path. The default bot is resolved to its synthesised spec
    so default-bot routines also run as turns.
    """

    from server.goal_run.bot_identity import DEFAULT_BOT_ID, BotSpec

    try:
        registry = bot_registry or BotRegistry()
        spec = registry.get(str(bot_id))
        if spec is not None:
            return spec
        if str(bot_id) == DEFAULT_BOT_ID:
            return BotSpec(bot_id=DEFAULT_BOT_ID, owner_id="system", name="Veya Bot",
                           description="The default Veya bot.", runner_id="local")
    except Exception:
        _lg.debug("could not resolve bot %s for routine", bot_id, exc_info=True)
    return None


async def _try_supervisor_handoff(*, routine_id: str, bot_id: str) -> bool:
    """Run a supervisor routine handoff if one is bound to the routine.

    Returns True when this routine was a supervisor routine (the caller
    should skip its normal handoff), False otherwise. Disabled by the
    VEYA_SUPERVISOR flag; also a no-op when the routine has no binding.
    """
    from server.supervisor import binding as supervisor_binding
    from server.supervisor import enabled as is_supervisor_enabled

    if not is_supervisor_enabled():
        return False

    binding = None
    try:
        for item in supervisor_binding.list_bindings(owner_id=bot_id):
            if item.routine_id == routine_id:
                binding = item
                break
    except Exception:
        _lg.debug("could not load supervisor bindings for %s", bot_id, exc_info=True)
        return False
    if binding is None:
        return False

    from server.supervisor.handoff import supervisor_handoff

    try:
        await supervisor_handoff(
            binding=binding,
            goal_run_id=binding.goal_run_id,
            project_root=binding.project_root,
        )
    except Exception:
        _lg.exception("supervisor handoff failed for routine %s", routine_id)
    return True


async def _run_bot_turn_handoff(
    *,
    bot_spec: "object",
    goal_text: str,
    routine_id: str,
    trigger_id: str,
    bot_registry=None,
) -> None:
    """Run a bot-owned routine as one bot turn and record the reply.

    Uses ``server.bot_team.turns.run_bot_turn`` — the single product entry the
    UI drives — so the bot's own answer is extracted and appended to its
    conversation. Any failure is reported, never swallowed silently.
    """

    from server.bot_team.turns import run_bot_turn
    from server.notification_center import global_notifier

    user = {"user_id": getattr(bot_spec, "owner_id", "") or "system", "username": ""}
    try:
        turn = await run_bot_turn(
            bot=bot_spec,
            text=str(goal_text or f"Run routine {routine_id}"),
            user=user,
            context=f"Routine {routine_id} fired.",
            reply_meta={"source": "routine", "routine_id": str(routine_id)},
        )
        summary = (turn.reply or "").splitlines()[0] if turn.reply else "Success"
        routed = _route_wake(
            bot_id=str(bot_spec.bot_id),
            routine_id=str(routine_id),
            trigger_id=str(trigger_id),
            summary=summary,
            bot_registry=bot_registry,
        )
        if routed is None:
            global_notifier.push(
                type="SUCCESS",
                title="Routine finished",
                content=summary,
                payload={"routine_id": routine_id},
                user_id="system"
            )
    except Exception as e:
        _lg.exception("Bot-turn routine %s failed", routine_id)
        routed = _route_wake(
            bot_id=str(bot_spec.bot_id),
            routine_id=str(routine_id),
            trigger_id=str(trigger_id),
            summary=str(e),
            error=str(e),
            bot_registry=bot_registry,
        )
        if routed is None:
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
            try:
                await self.dispatch_fn(handoffs)
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
