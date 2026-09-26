"""CLI handlers for Veya Agent Runtime, Triggers, Schedules, Outbox, and Dead-Letters."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from veya.agent_runtime import (
    AgentRuntime,
    AgentRuntimeStore,
    MissedSchedulePolicy,
    ScheduleType,
    TriggerType,
)


def _get_project_root() -> Path:
    return Path(os.environ.get("VEYA_PROJECT_ROOT", os.getcwd()))


def run_runtime_cli(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="veya runtime", description="Manage daemon Agent Runtime")
    sub = parser.add_subparsers(dest="subcommand", required=True)

    # status / health
    p_status = sub.add_parser("status", help="show runtime health and metrics")
    p_status.add_argument("--json", action="store_true")

    p_health = sub.add_parser("health", help="show runtime health and metrics")
    p_health.add_argument("--json", action="store_true")

    # recover
    p_rec = sub.add_parser("recover", help="run startup recovery sequence")
    p_rec.add_argument("--json", action="store_true")

    # drain
    p_drain = sub.add_parser("drain", help="drain runtime and stop")
    p_drain.add_argument("--timeout", type=float, default=5.0)
    p_drain.add_argument("--json", action="store_true")

    # run
    p_run = sub.add_parser("run", help="run long-lived daemon process")
    p_run.add_argument("--interval", type=float, default=0.1)

    args = parser.parse_args(argv)
    runtime = AgentRuntime(_get_project_root())

    if args.subcommand in ("status", "health"):
        health = runtime.get_health()
        if args.json:
            print(json.dumps(health.to_dict(), indent=2))
        else:
            print(f"Status: {health.status}")
            print(f"Runtime Generation: {health.runtime_generation}")
            print(f"Uptime: {health.uptime_seconds}s")
            print(f"Active Missions: {health.active_missions_count}")
            print(f"Pending Triggers: {health.pending_triggers_count}")
            print(f"Pending Deliveries: {health.pending_deliveries_count}")
            print(f"Dead Letters: {health.dead_letters_count}")
        return 0

    if args.subcommand == "recover":
        res = runtime.startup_recovery()
        if args.json:
            print(json.dumps(res, indent=2))
        else:
            print(
                f"Recovery complete. Generation: {res['generation']}, reclaimed leases: {res['reclaimed_leases']}, recovered inbox: {res['recovered_inbox_messages']}"
            )
        return 0

    if args.subcommand == "drain":
        asyncio.run(runtime.drain(args.timeout))
        if args.json:
            print(json.dumps({"status": str(runtime.status)}, indent=2))
        else:
            print(f"Runtime drained. Current status: {runtime.status}")
        return 0

    if args.subcommand == "run":
        asyncio.run(runtime.run_daemon(args.interval))
        return 0

    return 0


def run_trigger_cli(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="veya trigger", description="Inspect and manage Agent Triggers"
    )
    sub = parser.add_subparsers(dest="subcommand", required=True)

    # list
    p_list = sub.add_parser("list", help="list triggers")
    p_list.add_argument("--channel", default=None)
    p_list.add_argument("--status", default=None)
    p_list.add_argument("--json", action="store_true")

    # get
    p_get = sub.add_parser("get", help="get trigger details")
    p_get.add_argument("trigger_id", help="trigger ID")
    p_get.add_argument("--json", action="store_true")

    # create
    p_create = sub.add_parser("create", help="create an agent trigger")
    p_create.add_argument("--channel", required=True, help="channel ID")
    p_create.add_argument("--type", default="USER_MESSAGE", help="trigger type")
    p_create.add_argument("--principal", default="default", help="principal ID")
    p_create.add_argument("--content", default="", help="prompt or goal content")
    p_create.add_argument("--idempotency-key", default=None, help="idempotency key")
    p_create.add_argument("--json", action="store_true")

    args = parser.parse_args(argv)
    store = AgentRuntimeStore(_get_project_root())

    if args.subcommand == "list":
        triggers = store.list_triggers(args.channel, args.status)
        rows = [t.to_dict() for t in triggers]
        if args.json:
            print(json.dumps(rows, indent=2))
        else:
            for t in triggers:
                print(
                    f"{t.trigger_id:<22} channel={t.channel_id:<15} type={t.trigger_type:<14} status={t.status}"
                )
        return 0

    if args.subcommand == "get":
        t = store.get_trigger(args.trigger_id)
        if not t:
            err = {"error": "NOT_FOUND", "message": f"Trigger {args.trigger_id} not found"}
            print(
                json.dumps(err) if args.json else f"error: Trigger {args.trigger_id} not found",
                file=sys.stderr,
            )
            return 1
        if args.json:
            print(json.dumps(t.to_dict(), indent=2))
        else:
            print(
                f"Trigger: {t.trigger_id}\nChannel: {t.channel_id}\nType: {t.trigger_type}\nStatus: {t.status}\nPayload: {t.payload}"
            )
        return 0

    if args.subcommand == "create":
        runtime = AgentRuntime(_get_project_root(), store=store)
        trig = runtime.triggers.create_trigger(
            channel_id=args.channel,
            trigger_type=TriggerType(args.type.upper()),
            principal_id=args.principal,
            payload={"content": args.content, "prompt": args.content},
            idempotency_key=args.idempotency_key,
        )
        if args.json:
            print(json.dumps(trig.to_dict(), indent=2))
        else:
            print(f"Created trigger {trig.trigger_id} ({trig.status})")
        return 0

    return 0


def run_schedule_cli(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="veya schedule", description="Manage Agent Schedules")
    sub = parser.add_subparsers(dest="subcommand", required=True)

    # list
    p_list = sub.add_parser("list", help="list schedules")
    p_list.add_argument("--channel", default=None)
    p_list.add_argument("--json", action="store_true")

    # create
    p_create = sub.add_parser("create", help="create a schedule")
    p_create.add_argument("--channel", required=True)
    p_create.add_argument("--type", choices=["INTERVAL", "ONCE", "CRON"], default="INTERVAL")
    p_create.add_argument("--expression", required=True, help="interval seconds or cron expression")
    p_create.add_argument("--principal", default="default")
    p_create.add_argument(
        "--missed-policy", choices=["FIRE_ONCE", "SKIP", "CATCH_UP_LIMITED"], default="FIRE_ONCE"
    )
    p_create.add_argument("--json", action="store_true")

    # pause
    p_pause = sub.add_parser("pause", help="pause a schedule")
    p_pause.add_argument("schedule_id")
    p_pause.add_argument("--json", action="store_true")

    # resume
    p_resume = sub.add_parser("resume", help="resume a schedule")
    p_resume.add_argument("schedule_id")
    p_resume.add_argument("--json", action="store_true")

    args = parser.parse_args(argv)
    runtime = AgentRuntime(_get_project_root())

    if args.subcommand == "list":
        schedules = runtime.store.list_schedules(args.channel)
        rows = [s.to_dict() for s in schedules]
        if args.json:
            print(json.dumps(rows, indent=2))
        else:
            for s in schedules:
                state = "enabled" if s.enabled else "paused"
                print(
                    f"{s.schedule_id:<18} channel={s.channel_id:<15} type={s.schedule_type:<10} expr={s.expression:<12} ({state})"
                )
        return 0

    if args.subcommand == "create":
        sched = runtime.scheduler.create_schedule(
            channel_id=args.channel,
            schedule_type=ScheduleType(args.type),
            expression=args.expression,
            principal_id=args.principal,
            missed_policy=MissedSchedulePolicy(args.missed_policy),
        )
        if args.json:
            print(json.dumps(sched.to_dict(), indent=2))
        else:
            print(f"Created schedule {sched.schedule_id} (next fire at {sched.next_fire_at})")
        return 0

    if args.subcommand == "pause":
        ok = runtime.scheduler.set_enabled(args.schedule_id, False)
        if args.json:
            print(
                json.dumps(
                    {"schedule_id": args.schedule_id, "enabled": not ok if not ok else False}
                )
            )
        else:
            print(
                f"Schedule {args.schedule_id} paused"
                if ok
                else f"Schedule {args.schedule_id} not found"
            )
        return 0 if ok else 1

    if args.subcommand == "resume":
        ok = runtime.scheduler.set_enabled(args.schedule_id, True)
        if args.json:
            print(json.dumps({"schedule_id": args.schedule_id, "enabled": ok}))
        else:
            print(
                f"Schedule {args.schedule_id} resumed"
                if ok
                else f"Schedule {args.schedule_id} not found"
            )
        return 0 if ok else 1

    return 0


def run_outbox_cli(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="veya outbox", description="Inspect Notification Outbox")
    sub = parser.add_subparsers(dest="subcommand", required=True)

    p_list = sub.add_parser("list", help="list outbox deliveries")
    p_list.add_argument("--status", default=None)
    p_list.add_argument("--json", action="store_true")

    args = parser.parse_args(argv)
    store = AgentRuntimeStore(_get_project_root())

    if args.subcommand == "list":
        delivs = store.list_deliveries(args.status)
        rows = [d.to_dict() for d in delivs]
        if args.json:
            print(json.dumps(rows, indent=2))
        else:
            for d in delivs:
                print(
                    f"{d.delivery_id:<18} notif={d.notification_id:<16} sink={d.sink_id:<10} status={d.status} attempt={d.attempt}"
                )
        return 0

    return 0


def run_dead_letter_cli(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="veya dead-letter", description="Inspect and replay dead-letter queue"
    )
    sub = parser.add_subparsers(dest="subcommand", required=True)

    p_list = sub.add_parser("list", help="list dead letters")
    p_list.add_argument("--type", default=None)
    p_list.add_argument("--json", action="store_true")

    p_replay = sub.add_parser("replay", help="replay dead letter item")
    p_replay.add_argument("dead_letter_id")
    p_replay.add_argument("--json", action="store_true")

    args = parser.parse_args(argv)
    runtime = AgentRuntime(_get_project_root())

    if args.subcommand == "list":
        records = runtime.store.list_dead_letters(args.type)
        rows = [r.to_dict() for r in records]
        if args.json:
            print(json.dumps(rows, indent=2))
        else:
            for r in records:
                print(
                    f"{r.dead_letter_id:<16} entity={r.entity_type:<10} id={r.entity_id:<16} reason={r.reason}"
                )
        return 0

    if args.subcommand == "replay":
        res = runtime.outbox.replay_dead_letter(args.dead_letter_id)
        if not res:
            err = {
                "error": "FAILED",
                "message": f"Could not replay dead letter {args.dead_letter_id}",
            }
            print(
                json.dumps(err) if args.json else f"error: Could not replay {args.dead_letter_id}",
                file=sys.stderr,
            )
            return 1
        if args.json:
            print(json.dumps(res.to_dict(), indent=2))
        else:
            print(
                f"Replayed dead letter {args.dead_letter_id} into delivery {res.delivery_id} ({res.status})"
            )
        return 0

    return 0
