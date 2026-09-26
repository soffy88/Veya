"""CLI handlers for Channel, Agent Inbox, and Notification Projection."""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from pathlib import Path

from veya.channel import (
    AgentInbox,
    Channel,
    ChannelStore,
    ChannelType,
    InboxMessageType,
    NotificationProjector,
)


def _get_project_root() -> Path:
    return Path(os.environ.get("VEYA_PROJECT_ROOT", os.getcwd()))


def run_channel_cli(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="veya channel", description="Manage agent communication channels"
    )
    sub = parser.add_subparsers(dest="subcommand", required=True)

    # list
    p_list = sub.add_parser("list", help="list channels")
    p_list.add_argument("--status", choices=["ACTIVE", "PAUSED", "ARCHIVED"], default=None)
    p_list.add_argument("--json", action="store_true")

    # create
    p_create = sub.add_parser("create", help="create a new channel")
    p_create.add_argument("name", help="channel name")
    p_create.add_argument(
        "--type", choices=["web", "cli", "im", "webhook", "daemon"], default="cli"
    )
    p_create.add_argument("--workspace", default="")
    p_create.add_argument("--json", action="store_true")

    # get
    p_get = sub.add_parser("get", help="get channel details")
    p_get.add_argument("channel_id", help="channel ID")
    p_get.add_argument("--json", action="store_true")

    # archive
    p_arch = sub.add_parser("archive", help="archive a channel")
    p_arch.add_argument("channel_id", help="channel ID")
    p_arch.add_argument("--json", action="store_true")

    args = parser.parse_args(argv)
    store = ChannelStore(_get_project_root())

    if args.subcommand == "list":
        channels = store.list(args.status)
        rows = [c.to_dict() for c in channels]
        if args.json:
            print(json.dumps(rows, indent=2))
        else:
            for c in channels:
                print(f"{c.channel_id:<20} {c.name:<20} type={c.channel_type:<8} status={c.status}")
        return 0

    if args.subcommand == "create":
        cid = f"ch_{uuid.uuid4().hex[:12]}"
        ch = Channel(
            channel_id=cid,
            channel_type=ChannelType(args.type),
            name=args.name,
            workspace_root=args.workspace or str(_get_project_root()),
        )
        saved = store.save(ch)
        if args.json:
            print(json.dumps(saved.to_dict(), indent=2))
        else:
            print(f"Created channel {saved.channel_id} ({saved.name})")
        return 0

    if args.subcommand == "get":
        ch = store.get(args.channel_id)
        if ch is None:
            err = {"error": "NOT_FOUND", "message": f"Channel {args.channel_id} not found"}
            print(
                json.dumps(err) if args.json else f"error: Channel {args.channel_id} not found",
                file=sys.stderr,
            )
            return 1
        if args.json:
            print(json.dumps(ch.to_dict(), indent=2))
        else:
            print(
                f"Channel: {ch.channel_id}\nName: {ch.name}\nType: {ch.channel_type}\nStatus: {ch.status}\nWorkspace: {ch.workspace_root}"
            )
        return 0

    if args.subcommand == "archive":
        try:
            archived = store.archive(args.channel_id)
            if args.json:
                print(json.dumps(archived.to_dict(), indent=2))
            else:
                print(f"Archived channel {archived.channel_id}")
            return 0
        except KeyError as exc:
            err = {"error": "NOT_FOUND", "message": str(exc)}
            print(json.dumps(err) if args.json else f"error: {exc}", file=sys.stderr)
            return 1

    return 2


def run_inbox_cli(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="veya inbox", description="Manage Agent Inbox messages and notifications"
    )
    sub = parser.add_subparsers(dest="subcommand", required=True)

    # list / poll
    p_list = sub.add_parser("list", help="list or poll inbox messages")
    p_list.add_argument("--channel", default=None, help="channel ID")
    p_list.add_argument(
        "--status",
        choices=["PENDING", "DISPATCHED", "PROCESSED", "FAILED", "ARCHIVED"],
        default="PENDING",
    )
    p_list.add_argument("--limit", type=int, default=20)
    p_list.add_argument("--json", action="store_true")

    # send / enqueue
    p_send = sub.add_parser("send", help="send/enqueue a message into a channel inbox")
    p_send.add_argument("channel_id", help="target channel ID")
    p_send.add_argument("content", help="message content")
    p_send.add_argument("--sender", default="user")
    p_send.add_argument(
        "--type",
        choices=["user_prompt", "webhook_event", "interrupt_reply", "schedule_trigger"],
        default="user_prompt",
    )
    p_send.add_argument("--idempotency-key", default=None)
    p_send.add_argument("--json", action="store_true")

    # notifications
    p_notif = sub.add_parser("notifications", help="list notifications projected to a channel")
    p_notif.add_argument("channel_id", help="channel ID")
    p_notif.add_argument("--limit", type=int, default=20)
    p_notif.add_argument("--json", action="store_true")

    args = parser.parse_args(argv)
    root = _get_project_root()
    inbox = AgentInbox(root)

    if args.subcommand == "list":
        messages = inbox.poll(channel_id=args.channel, status=args.status, limit=args.limit)
        rows = [m.to_dict() for m in messages]
        if args.json:
            print(json.dumps(rows, indent=2))
        else:
            for m in messages:
                print(
                    f"{m.message_id:<20} channel={m.channel_id:<16} status={m.status:<12} sender={m.sender:<10} {m.content[:40]}"
                )
        return 0

    if args.subcommand == "send":
        msg = inbox.enqueue(
            channel_id=args.channel_id,
            content=args.content,
            sender=args.sender,
            message_type=InboxMessageType(args.type),
            idempotency_key=args.idempotency_key,
        )
        if args.json:
            print(json.dumps(msg.to_dict(), indent=2))
        else:
            print(f"Enqueued message {msg.message_id} to channel {msg.channel_id}")
        return 0

    if args.subcommand == "notifications":
        projector = NotificationProjector(root)
        notifs = projector.list_notifications(args.channel_id, limit=args.limit)
        rows = [n.to_dict() for n in notifs]
        if args.json:
            print(json.dumps(rows, indent=2))
        else:
            for n in notifs:
                print(f"[{n.level}] {n.title} - {n.body[:50]}")
        return 0

    return 2


__all__ = ["run_channel_cli", "run_inbox_cli"]
