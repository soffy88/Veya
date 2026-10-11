#!/usr/bin/env python3
"""scripts/veya_cli.py — the agent-first Veya CLI (NANO-W9 element 6).

Designed to be driven by an LLM tool call, so every command obeys the
CLI-Anything harness conventions:

* ``--json`` on every command → one stable envelope on stdout:
  ``{"status": "ok"|"error", ...}``; errors are
  ``{"status": "error", "error": {"code", "message"}}`` with a **non-zero exit**;
* bare invocation (no subcommand) enters an interactive REPL;
* ``--help`` is self-describing, including the JSON schema of each command;
* a command never prints anything except its JSON envelope on stdout, so an
  agent can parse it unconditionally.

Two transports, same envelope:

* **in-process** (default) — imports ``server.nano_w9`` directly; no server
  needed, which is what makes it usable from a bare checkout;
* **http** (``--base-url`` / ``VEYA_BASE_URL``) — talks to the existing REST API
  with ``Authorization: Bearer $VEYA_TOKEN`` (the existing token mechanism,
  ``server.auth.resolve_token``).

``skills/veya-cli/SKILL.md`` is **generated** from this file's argparse tree by
``scripts/gen_veya_cli_skill.py``; ``tests/test_nano_w9_cli_harness.py`` fails
when the two drift.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shlex
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

__version__ = "0.1.0"

# Allow `python scripts/veya_cli.py ...` from anywhere in the repo: the in-process
# transport imports ``server.*`` and ``veya.*``, which live beside ``scripts/``.
_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

# Imported after the bootstrap on purpose: this is a repo-local package.
from server.nano_w9.flags import FeatureDisabled  # noqa: E402

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2

# --------------------------------------------------------------------------
# envelope
# --------------------------------------------------------------------------


def ok_envelope(**fields: Any) -> dict[str, Any]:
    """Build the success envelope.

    Args:
        **fields: command payload merged into the envelope.

    Returns:
        ``{"status": "ok", **fields}``.

    Example:
        >>> ok_envelope(count=1)
        {'status': 'ok', 'count': 1}
    """
    return {"status": "ok", **fields}


def merge_ok(result: dict[str, Any], **extra: Any) -> dict[str, Any]:
    """Merge a result dict into a success envelope without letting it shadow
    the envelope's own ``status`` key.

    Args:
        result: a command result that may itself carry ``status``.
        **extra: additional envelope fields.

    Returns:
        ``{"status": "ok", **result-without-status, **extra}``.

    Example:
        >>> merge_ok({"status": "empty", "n": 1})
        {'status': 'ok', 'n': 1}
    """
    payload = {k: v for k, v in (result or {}).items() if k != "status"}
    return ok_envelope(**payload, **extra)


def error_envelope(code: str, message: str, **fields: Any) -> dict[str, Any]:
    """Build the error envelope.

    Args:
        code: stable machine-readable code (e.g. ``FEATURE_DISABLED``).
        message: human-readable detail.
        **fields: extra diagnostic fields.

    Returns:
        ``{"status": "error", "error": {"code", "message", **fields}}``.

    Example:
        >>> error_envelope("X", "y")["error"]["code"]
        'X'
    """
    return {"status": "error", "error": {"code": code, "message": message, **fields}}


def emit(payload: dict[str, Any], *, as_json: bool = True, stream: Any = None) -> int:
    """Print one envelope and return the process exit code.

    Args:
        payload: the envelope from :func:`ok_envelope` / :func:`error_envelope`.
        as_json: when False a one-line human summary is printed instead (REPL use).
        stream: output stream (defaults to stdout).

    Returns:
        ``EXIT_OK`` for a success envelope, ``EXIT_ERROR`` otherwise.

    Example:
        >>> emit(ok_envelope(x=1), stream=open("/dev/null", "w"))
        0
    """
    out = stream if stream is not None else sys.stdout
    if as_json:
        out.write(json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str) + "\n")
    else:
        if payload.get("status") == "ok":
            extra = {k: v for k, v in payload.items() if k != "status"}
            out.write("ok " + json.dumps(extra, ensure_ascii=False, default=str) + "\n")
        else:
            err = payload.get("error") or {}
            out.write(f"error {err.get('code', 'UNKNOWN')}: {err.get('message', '')}\n")
    out.flush()
    return EXIT_OK if payload.get("status") == "ok" else EXIT_ERROR


def run_async(coro: Any) -> Any:
    """Run a coroutine to completion (``asyncio.run``)."""
    return asyncio.run(coro)


# --------------------------------------------------------------------------
# transport
# --------------------------------------------------------------------------


class Transport:
    """Where a command actually executes."""

    def __init__(self, *, base_url: str = "", token: str = "") -> None:
        self.base_url = (base_url or os.environ.get("VEYA_BASE_URL", "")).rstrip("/")
        self.token = token or os.environ.get("VEYA_TOKEN", "")

    @property
    def remote(self) -> bool:
        """True when commands should go over HTTP instead of in-process."""
        return bool(self.base_url)

    def headers(self) -> dict[str, str]:
        """Auth headers for the existing bearer mechanism.

        Returns:
            ``{"Authorization": "Bearer <token>"}`` when a token is present,
            otherwise ``{}`` (the API then resolves to ``anonymous`` exactly as
            an unauthenticated HTTP client would).
        """
        if not self.token:
            return {}
        return {"Authorization": f"Bearer {self.token}"}

    def get(self, path: str, **params: Any) -> dict[str, Any]:
        """HTTP GET against the existing API."""
        import httpx

        clean = {k: v for k, v in params.items() if v not in (None, "", [], {})}
        with httpx.Client(timeout=30.0) as client:
            response = client.get(f"{self.base_url}{path}", params=clean, headers=self.headers())
        if response.status_code >= 400:
            return error_envelope(
                "HTTP_ERROR", f"{response.status_code} from {path}", status=response.status_code
            )
        return ok_envelope(result=response.json())

    def post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        """HTTP POST against the existing API."""
        import httpx

        with httpx.Client(timeout=60.0) as client:
            response = client.post(f"{self.base_url}{path}", json=body, headers=self.headers())
        if response.status_code >= 400:
            return error_envelope(
                "HTTP_ERROR",
                f"{response.status_code} from {path}",
                status=response.status_code,
                detail=_safe_detail(response),
            )
        return ok_envelope(result=response.json())


def _safe_detail(response: Any) -> Any:
    try:
        return response.json().get("detail")
    except Exception:
        return None


# --------------------------------------------------------------------------
# commands — each returns an envelope
# --------------------------------------------------------------------------


def cmd_flags(transport: Transport, args: argparse.Namespace) -> dict[str, Any]:
    """Report every NANO-W9 feature flag."""
    if transport.remote:
        return transport.get("/api/v1/nano-w9/flags")
    from server.nano_w9.flags import feature_flags

    return ok_envelope(flags=feature_flags())


def cmd_chat_send(transport: Transport, args: argparse.Namespace) -> dict[str, Any]:
    """Send a message to a bot or a group."""
    payload = {"text": args.text, "bot_id": args.bot or "", "group_id": args.group or ""}
    if transport.remote:
        if args.group:
            return transport.post(f"/api/v1/bots/groups/{args.group}/messages", payload)
        if args.bot:
            return transport.post(f"/api/v1/bots/{args.bot}/messages", payload)
        return error_envelope("INVALID_INPUT", "--bot or --group is required")
    from server.bot_team.groups import GroupStore

    store = GroupStore()
    group_id = args.group
    if not group_id:
        for group in store.list():
            if args.bot and args.bot in group.bot_ids:
                group_id = group.id
                break
    if not group_id:
        return error_envelope("INVALID_INPUT", "no group matched --bot/--group")
    return ok_envelope(result=store.append_message(group_id, role="user", text=args.text))


def cmd_chat_messages(transport: Transport, args: argparse.Namespace) -> dict[str, Any]:
    """List messages in a group."""
    if transport.remote:
        return transport.get(f"/api/v1/bots/groups/{args.group}/messages")
    from server.bot_team.groups import GroupStore

    return ok_envelope(messages=GroupStore().messages(args.group))


def cmd_group_open(transport: Transport, args: argparse.Namespace) -> dict[str, Any]:
    """Open (or reuse) the room for a branch."""
    body = {"branch": args.branch, "bot_ids": args.bot or [], "repo_path": args.repo or ""}
    if transport.remote:
        return transport.post("/api/v1/nano-w9/branch-rooms/open", body)
    from server.routes.nano_w9 import _branch_rooms_facade

    try:
        result = run_async(
            _branch_rooms_facade().open_room(
                args.branch, bot_ids=args.bot or [], repo_path=args.repo or ""
            )
        )
    except FeatureDisabled as exc:
        return error_envelope(exc.as_error()["code"], "branch rooms are disabled", flag=exc.flag)
    if result.get("status") != "completed":
        error = result.get("error") or {}
        return error_envelope(str(error.get("code") or "FAILED"), str(error.get("reason") or ""))
    return merge_ok(result)


def cmd_group_publish(transport: Transport, args: argparse.Namespace) -> dict[str, Any]:
    """Publish branch facts into a room."""
    body = {
        "branch": args.branch,
        "group_id": args.group,
        "repo_path": args.repo or "",
        "goal_id": args.goal or "",
        "project_root": args.project_root or ".",
        "merge_decision": args.decision or "",
    }
    if transport.remote:
        return transport.post("/api/v1/nano-w9/branch-rooms/publish", body)
    from server.routes.nano_w9 import _branch_rooms_facade

    try:
        result = run_async(_branch_rooms_facade().publish(**body))
    except FeatureDisabled as exc:
        return error_envelope(exc.as_error()["code"], "branch rooms are disabled", flag=exc.flag)
    if result.get("status") != "completed":
        error = result.get("error") or {}
        return error_envelope(str(error.get("code") or "FAILED"), str(error.get("reason") or ""))
    return merge_ok(result)


def cmd_group_list(transport: Transport, args: argparse.Namespace) -> dict[str, Any]:
    """List branch rooms."""
    if transport.remote:
        return transport.get("/api/v1/nano-w9/branch-rooms")
    from server.routes.nano_w9 import _branch_rooms_facade

    try:
        return ok_envelope(rooms=_branch_rooms_facade().list_rooms())
    except FeatureDisabled as exc:
        return error_envelope(exc.as_error()["code"], "branch rooms are disabled", flag=exc.flag)


def cmd_goal_start(transport: Transport, args: argparse.Namespace) -> dict[str, Any]:
    """Start a GoalRun (enqueue only; never blocks the CLI)."""
    if transport.remote:
        return transport.post("/api/v1/goal/start", {"goal": args.goal})
    from server.goal_run.runner import project_run_goal

    try:
        result = run_async(project_run_goal(args.project_root, args.goal, wait=False))
    except Exception as exc:
        return error_envelope(type(exc).__name__, str(exc))
    return ok_envelope(result=result if isinstance(result, dict) else {"goal_id": str(result)})


def cmd_goal_status(transport: Transport, args: argparse.Namespace) -> dict[str, Any]:
    """Read a GoalRun's status (read-only)."""
    if transport.remote:
        return transport.get("/api/v1/goal/status", goal_id=args.goal_id)
    from server.goal_run.status import project_goal_status

    try:
        result = run_async(project_goal_status(args.project_root, args.goal_id))
    except Exception as exc:
        return error_envelope(type(exc).__name__, str(exc))
    return ok_envelope(result=result if isinstance(result, dict) else {"status": str(result)})


def cmd_routine_list(transport: Transport, args: argparse.Namespace) -> dict[str, Any]:
    """List registered routines."""
    if transport.remote:
        return transport.get("/api/v1/bots/default/routines")
    from server.goal_run.routine_registry import RoutineRegistry

    registry = RoutineRegistry()
    return ok_envelope(routines=[s.to_dict() for s in registry.list(include_disabled=True)])


def cmd_routine_signal(transport: Transport, args: argparse.Namespace) -> dict[str, Any]:
    """Feed a signal to the routine trigger router."""
    body = {"kind": args.kind, "text": args.text or "", "reaction": args.reaction or ""}
    if transport.remote:
        return transport.post("/api/v1/nano-w9/signals", body)
    from server.routes.nano_w9 import _routine_router

    try:
        result = run_async(_routine_router().handle(body))
    except FeatureDisabled as exc:
        return error_envelope(
            exc.as_error()["code"], "routine triggers are disabled", flag=exc.flag
        )
    return merge_ok(result)


def cmd_search(transport: Transport, args: argparse.Namespace) -> dict[str, Any]:
    """Search with receipts across sessions/goals/groups/approvals/annotations."""
    body = {"query": args.query, "limit": args.limit, "sources": args.source or []}
    if transport.remote:
        return transport.post("/api/v1/nano-w9/search", body)
    from server.nano_w9.search_receipts import ReceiptSearch, default_sources

    sources = default_sources()
    if args.source:
        sources = {k: v for k, v in sources.items() if k in args.source}
    result = ReceiptSearch(sources=sources).answer(args.query, limit=args.limit)
    return merge_ok(result)


def cmd_audit_verify(transport: Transport, args: argparse.Namespace) -> dict[str, Any]:
    """Verify the append-only audit hash chain."""
    if transport.remote:
        return transport.get("/api/v1/nano-w9/audit/verify")
    from server.nano_w9.audit_chain import verify_audit_chain

    result = verify_audit_chain()
    if not result.get("ok"):
        return error_envelope("CHAIN_BROKEN", str(result.get("reason") or ""), **result)
    return merge_ok(result)


def cmd_audit_records(transport: Transport, args: argparse.Namespace) -> dict[str, Any]:
    """List the most recent chained audit records."""
    if transport.remote:
        return transport.get("/api/v1/nano-w9/audit/verify")
    from server.nano_w9.audit_chain import audit_envelopes

    return ok_envelope(records=audit_envelopes(limit=args.limit))


def cmd_trajectory_record(transport: Transport, args: argparse.Namespace) -> dict[str, Any]:
    """Record one artifact-producing step on the existing event log."""
    body = {
        "step": args.step,
        "command": args.command,
        "artifact_ref": args.artifact or "",
        "preview_ref": args.preview or "",
        "goal_id": args.goal or "",
        "kind": args.kind or "file",
    }
    if transport.remote:
        return transport.post("/api/v1/nano-w9/trajectory", body)
    from server.nano_w9.trajectory import Trajectory

    result = Trajectory().record(**body)
    if result.get("status") != "ok":
        error = result.get("error") or {}
        return error_envelope(str(error.get("code") or "FAILED"), str(error.get("reason") or ""))
    return merge_ok(result)


def cmd_trajectory_replay(transport: Transport, args: argparse.Namespace) -> dict[str, Any]:
    """Replay a run's command → artifact states."""
    if transport.remote:
        return transport.get(f"/api/v1/nano-w9/trajectory/{args.goal}")
    from server.nano_w9.trajectory import read_trajectory

    return merge_ok(read_trajectory(args.goal))


def cmd_acp_members(transport: Transport, args: argparse.Namespace) -> dict[str, Any]:
    """List ACP members and candidates."""
    if transport.remote:
        return transport.get("/api/v1/nano-w9/acp/members")
    from server.routes.nano_w9 import _acp_members

    try:
        facade = _acp_members()
    except FeatureDisabled as exc:
        return error_envelope(exc.as_error()["code"], "ACP members are disabled", flag=exc.flag)
    return ok_envelope(members=facade.members(), candidates=facade.candidates())


def cmd_acp_add(transport: Transport, args: argparse.Namespace) -> dict[str, Any]:
    """Register an ACP backend as a group/bot member."""
    body = {"backend_id": args.backend, "owner_id": args.owner or ""}
    if transport.remote:
        return transport.post("/api/v1/nano-w9/acp/members", body)
    from server.routes.nano_w9 import _acp_members

    try:
        result = _acp_members().register_member(args.backend, owner_id=args.owner or "")
    except FeatureDisabled as exc:
        return error_envelope(exc.as_error()["code"], "ACP members are disabled", flag=exc.flag)
    if result.get("status") != "ok":
        error = result.get("error") or {}
        return error_envelope(str(error.get("code") or "FAILED"), str(error.get("reason") or ""))
    return merge_ok(result)


def cmd_acp_turn(transport: Transport, args: argparse.Namespace) -> dict[str, Any]:
    """Forward one turn to an ACP member through the existing GoalRun."""
    body = {"member_id": args.member, "prompt": args.prompt, "goal_id": args.goal or ""}
    if transport.remote:
        return transport.post("/api/v1/nano-w9/acp/turn", body)
    from server.routes.nano_w9 import _acp_members

    try:
        result = run_async(
            _acp_members().forward_turn(
                member_id=args.member, prompt=args.prompt, goal_id=args.goal or ""
            )
        )
    except FeatureDisabled as exc:
        return error_envelope(exc.as_error()["code"], "ACP members are disabled", flag=exc.flag)
    if result.get("status") != "ok":
        error = result.get("error") or {}
        return error_envelope(str(error.get("code") or "FAILED"), str(error.get("reason") or ""))
    return merge_ok(result)


def cmd_cli_gen(transport: Transport, args: argparse.Namespace) -> dict[str, Any]:
    """Run the 7-phase agent-native CLI generation flow (experimental)."""
    if transport.remote:
        return transport.post("/api/v1/nano-w9/cli-gen", {"repo_ref": args.repo})
    from server.nano_w9.cli_gen import run_cli_gen

    result = run_async(run_cli_gen(repo_ref=args.repo))
    if result.get("status") == "disabled":
        error = result.get("error") or {}
        return error_envelope(str(error.get("code") or "DISABLED"), "cli-gen is disabled")
    return merge_ok(result)


# --------------------------------------------------------------------------
# argparse tree (the single source SKILL.md is generated from)
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    """Build the full command tree.

    Returns:
        The root :class:`argparse.ArgumentParser`.  ``veya_cli.py`` and
        ``scripts/gen_veya_cli_skill.py`` both read this, so the documented CLI
        and the real CLI cannot drift.

    Example:
        >>> p = build_parser(); p.prog
        'veya'
    """
    parser = argparse.ArgumentParser(
        prog="veya",
        description=(
            "Agent-first Veya CLI. Every command accepts --json and prints "
            '{"status": "ok"|"error", ...}. Errors exit non-zero. '
            "Auth: VEYA_TOKEN / --token (Authorization: Bearer). "
            "Remote: --base-url / VEYA_BASE_URL (otherwise in-process)."
        ),
        epilog="Run with no subcommand to enter the interactive REPL.",
    )
    parser.add_argument("--version", action="version", version=f"veya-cli {__version__}")
    parser.add_argument("--base-url", default="", help="REST base URL (default: in-process)")
    parser.add_argument("--token", default="", help="bearer token (or VEYA_TOKEN)")
    parser.add_argument("--json", action="store_true", help="always emit the JSON envelope")
    parser.add_argument("--pretty", action="store_true", help="human summary instead of JSON")

    # ``--json``/``--pretty`` are accepted after the subcommand too, so an agent
    # never has to remember argument position.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    common.add_argument("--pretty", action="store_true", default=argparse.SUPPRESS)

    groups = parser.add_subparsers(dest="group", metavar="<group>")

    flags = groups.add_parser(
        "flags",
        parents=[common],
        help="show feature flags",
        description="Report every NANO-W9 feature flag and whether it is on.",
    )
    flags.set_defaults(handler=cmd_flags, schema='{"status","flags"}')

    chat = groups.add_parser("chat", parents=[common], help="send/list messages")
    chat_sub = chat.add_subparsers(dest="command", metavar="<command>")
    chat_send = chat_sub.add_parser(
        "send",
        parents=[common],
        help="send a message to a bot or group",
        description="Post TEXT into a P2 group as the user role.",
    )
    chat_send.add_argument("text")
    chat_send.add_argument("--bot", default="")
    chat_send.add_argument("--group", default="")
    chat_send.set_defaults(handler=cmd_chat_send, schema='{"status","result"}')
    chat_msgs = chat_sub.add_parser(
        "messages",
        parents=[common],
        help="list group messages",
        description="Read back a group transcript (GROUP id).",
    )
    chat_msgs.add_argument("group")
    chat_msgs.set_defaults(handler=cmd_chat_messages, schema='{"status","messages"}')

    room = groups.add_parser("room", parents=[common], help="branch rooms")
    room_sub = room.add_subparsers(dest="command", metavar="<command>")
    room_open = room_sub.add_parser(
        "open",
        parents=[common],
        help="open the room for a branch",
        description="Create or reuse the branch:<name> P2 room.",
    )
    room_open.add_argument("branch")
    room_open.add_argument("--bot", action="append", default=[])
    room_open.add_argument("--repo", default="")
    room_open.set_defaults(handler=cmd_group_open, schema='{"status","group_id","room_name"}')
    room_pub = room_sub.add_parser(
        "publish",
        parents=[common],
        help="publish branch facts into a room",
        description="Post commits, CI results, annotations, AI review and the merge decision.",
    )
    room_pub.add_argument("--branch", required=True)
    room_pub.add_argument("--group", required=True)
    room_pub.add_argument("--repo", default="")
    room_pub.add_argument("--goal", default="")
    room_pub.add_argument("--project-root", default=".")
    room_pub.add_argument("--decision", default="")
    room_pub.set_defaults(handler=cmd_group_publish, schema='{"status","posted","skipped"}')
    room_list = room_sub.add_parser(
        "list",
        parents=[common],
        help="list branch rooms",
        description="List rooms whose name carries the branch: prefix.",
    )
    room_list.set_defaults(handler=cmd_group_list, schema='{"status","rooms"}')

    goal = groups.add_parser("goal", parents=[common], help="GoalRun control")
    goal_sub = goal.add_subparsers(dest="command", metavar="<command>")
    goal_start = goal_sub.add_parser(
        "start",
        parents=[common],
        help="enqueue a GoalRun",
        description="Enqueue a GoalRun through the existing runner (never blocks).",
    )
    goal_start.add_argument("goal")
    goal_start.add_argument("--project-root", default=".")
    goal_start.set_defaults(handler=cmd_goal_start, schema='{"status","result"}')
    goal_status = goal_sub.add_parser(
        "status",
        parents=[common],
        help="read a GoalRun status",
        description="Read a GoalRun status; read-only, never mutates.",
    )
    goal_status.add_argument("goal_id")
    goal_status.add_argument("--project-root", default=".")
    goal_status.set_defaults(handler=cmd_goal_status, schema='{"status","result"}')

    routine = groups.add_parser("routine", parents=[common], help="routines and triggers")
    routine_sub = routine.add_subparsers(dest="command", metavar="<command>")
    routine_list = routine_sub.add_parser(
        "list",
        parents=[common],
        help="list routines",
        description="List every registered routine, disabled included.",
    )
    routine_list.set_defaults(handler=cmd_routine_list, schema='{"status","routines"}')
    routine_sig = routine_sub.add_parser(
        "signal",
        parents=[common],
        help="feed a signal to the trigger router",
        description="Route one webhook/message/reaction signal to the routines it fires.",
    )
    routine_sig.add_argument("kind", choices=["webhook", "message", "reaction"])
    routine_sig.add_argument("--text", default="")
    routine_sig.add_argument("--reaction", default="")
    routine_sig.set_defaults(handler=cmd_routine_signal, schema='{"status","fired"}')

    search = groups.add_parser(
        "search",
        parents=[common],
        help="search with receipts",
        description='Answer "have we seen this before?" with a cited answer.',
    )
    search.add_argument("query")
    search.add_argument("--limit", type=int, default=20)
    search.add_argument("--source", action="append", default=[])
    search.set_defaults(handler=cmd_search, schema='{"status","answer","receipts","cited"}')

    audit = groups.add_parser("audit", parents=[common], help="hash-chained audit")
    audit_sub = audit.add_subparsers(dest="command", metavar="<command>")
    audit_verify = audit_sub.add_parser(
        "verify",
        parents=[common],
        help="verify the chain",
        description="Re-derive every audit hash link; reports the first break.",
    )
    audit_verify.set_defaults(handler=cmd_audit_verify, schema='{"status","ok","length"}')
    audit_records = audit_sub.add_parser(
        "records",
        parents=[common],
        help="list chained records",
        description="Show the most recent chained audit records.",
    )
    audit_records.add_argument("--limit", type=int, default=50)
    audit_records.set_defaults(handler=cmd_audit_records, schema='{"status","records"}')

    traj = groups.add_parser("trajectory", parents=[common], help="preview + trajectory")
    traj_sub = traj.add_subparsers(dest="command", metavar="<command>")
    traj_rec = traj_sub.add_parser(
        "record",
        parents=[common],
        help="record one artifact step",
        description="Emit a preview bundle + trajectory entry on the existing event log.",
    )
    traj_rec.add_argument("--step", type=int, required=True)
    traj_rec.add_argument("--command", required=True)
    traj_rec.add_argument("--artifact", default="")
    traj_rec.add_argument("--preview", default="")
    traj_rec.add_argument("--goal", default="")
    traj_rec.add_argument("--kind", default="file")
    traj_rec.set_defaults(handler=cmd_trajectory_record, schema='{"status","trajectory"}')
    traj_rep = traj_sub.add_parser(
        "replay",
        parents=[common],
        help="replay command → artifact states",
        description="Walk a run into ordered command -> artifact states.",
    )
    traj_rep.add_argument("goal")
    traj_rep.set_defaults(handler=cmd_trajectory_replay, schema='{"status","states","artifacts"}')

    acp = groups.add_parser("acp", parents=[common], help="ACP members")
    acp_sub = acp.add_subparsers(dest="command", metavar="<command>")
    acp_list = acp_sub.add_parser(
        "members",
        parents=[common],
        help="list members and candidates",
        description="List registered ACP members and detectable ACP agents.",
    )
    acp_list.set_defaults(handler=cmd_acp_members, schema='{"status","members","candidates"}')
    acp_add = acp_sub.add_parser(
        "add",
        parents=[common],
        help="register an ACP backend as a member",
        description="Register an ACP backend as a P1 bot (a group member).",
    )
    acp_add.add_argument("backend")
    acp_add.add_argument("--owner", default="")
    acp_add.set_defaults(handler=cmd_acp_add, schema='{"status","member"}')
    acp_turn = acp_sub.add_parser(
        "turn",
        parents=[common],
        help="forward a turn through GoalRun",
        description="Forward one turn to an ACP member inside the existing GoalRun.",
    )
    acp_turn.add_argument("member")
    acp_turn.add_argument("prompt")
    acp_turn.add_argument("--goal", default="")
    acp_turn.set_defaults(handler=cmd_acp_turn, schema='{"status","goal_id"}')

    gen = groups.add_parser(
        "cli-gen",
        parents=[common],
        help="agent-native CLI generation (experimental)",
        description="Run the 7-phase agent-native flow (gated by VEYA_CLI_GEN).",
    )
    gen.add_argument("--repo", required=True)
    gen.set_defaults(handler=cmd_cli_gen, schema='{"status","phases","experimental"}')

    return parser


def command_index(parser: argparse.ArgumentParser | None = None) -> list[dict[str, Any]]:
    """Flatten the argparse tree into the rows SKILL.md documents.

    Args:
        parser: the root parser (built when omitted).

    Returns:
        ``[{"path", "help", "schema", "args"}]`` for every leaf command, sorted
        by ``path``.  This is the single source both the generator and the sync
        test consume.
    """
    root = parser or build_parser()
    rows: list[dict[str, Any]] = []

    def walk(prefix: str, sub: Any) -> None:
        actions = (
            [a for a in sub._subparsers._group_actions if hasattr(a, "choices")]
            if getattr(sub, "_subparsers", None)
            else []
        )
        for action in actions:
            for name, child in action.choices.items():
                path = f"{prefix} {name}".strip()
                grandchildren = [
                    a
                    for a in (
                        child._subparsers._group_actions
                        if getattr(child, "_subparsers", None)
                        else []
                    )
                    if hasattr(a, "choices")
                ]
                if grandchildren:
                    walk(path, child)
                    continue
                rows.append(
                    {
                        "path": path,
                        "help": child.description or "",
                        "schema": _schema_of(child),
                        "args": _render_args(child),
                    }
                )

    walk("", root)
    rows.sort(key=lambda r: r["path"])
    return rows


#: Output flags that apply to every command; documented once, not per command.
_GLOBAL_FLAGS = frozenset({"json", "pretty"})


def _render_args(parser: argparse.ArgumentParser) -> list[str]:
    """Render one leaf parser's arguments as agent-readable tokens.

    Args:
        parser: the leaf parser.

    Returns:
        ``["BRANCH", "--repo REPO", ...]`` — positionals bare, optionals with
        their value placeholder, global output flags omitted.

    Example:
        >>> _render_args(_subparser(build_parser(), "room open"))[:2]
        ['BRANCH', '--bot BOT']
    """
    out: list[str] = []
    for opt in parser._actions:
        if opt.dest in {"help", *_GLOBAL_FLAGS}:
            continue
        takes_value = (
            opt.nargs != 0 and not isinstance(getattr(opt, "const", None), bool)
        ) or opt.nargs in {"1", "?", "+", "*"}
        if opt.option_strings:
            flag = opt.option_strings[0]
            out.append(f"{flag} {opt.dest.upper()}" if takes_value else flag)
        else:
            out.append(opt.dest.upper())
    return out


def _schema_of(parser: argparse.ArgumentParser) -> str:
    """Read the ``schema`` hint a command registered via ``set_defaults``."""
    action = getattr(parser, "_defaults", None) or {}
    return str(action.get("schema") or "")


# --------------------------------------------------------------------------
# REPL
# --------------------------------------------------------------------------

REPL_HELP = (
    "veya REPL. Type a command with --json, e.g. `flags --json`. "
    "`quit` or Ctrl-D exits. `help <group>` explains a group."
)


def run_repl(transport: Transport, *, stdin: Any = None, stream: Any = None) -> int:
    """Interactive REPL entered by a bare invocation.

    Args:
        transport: the transport every command reuses.
        stdin: input stream (defaults to ``sys.stdin``).
        stream: output stream (defaults to ``sys.stdout``).

    Returns:
        A process exit code (``EXIT_OK`` when the session ends cleanly).
    """
    source = stdin if stdin is not None else sys.stdin
    out = stream if stream is not None else sys.stdout
    out.write(REPL_HELP + "\n")
    out.flush()
    last = EXIT_OK
    for line in source:
        text = line.strip()
        if not text:
            continue
        if text in {"quit", "exit", ":q"}:
            break
        try:
            argv = shlex.split(text)
        except ValueError:
            out.write(f"error USAGE: cannot parse {text!r}\n")
            out.flush()
            last = EXIT_USAGE
            continue
        if argv and argv[0] == "help":
            # ``help`` with no argument prints the root help; ``help <group>``
            # prints that group's help so the REPL is self-describing too.
            target = build_parser()
            if len(argv) > 1:
                target = _subparser(target, argv[1]) or target
            target.print_help(file=out)
            out.flush()
            continue
        last = dispatch(argv, transport, as_json=True, stream=out)
    return last


def _subparser(parser: argparse.ArgumentParser, name: str) -> argparse.ArgumentParser | None:
    """Find a nested subparser by path (``"room"``, ``"room open"``).

    Args:
        parser: the root parser.
        name: space-separated group/command path.

    Returns:
        The matching parser, or ``None`` when the path does not exist.

    Example:
        >>> _subparser(build_parser(), "room open") is not None
        True
        >>> _subparser(build_parser(), "nope") is None
        True
    """
    current: argparse.ArgumentParser | None = parser
    for part in name.split():
        subparsers = getattr(current, "_subparsers", None)
        if subparsers is None:
            return None
        match = None
        for action in subparsers._group_actions:
            if not hasattr(action, "choices"):
                continue
            match = action.choices.get(part)
            if match is not None:
                break
        if match is None:
            return None
        current = match
    return current


def dispatch(
    argv: list[str], transport: Transport, *, as_json: bool = True, stream: Any = None
) -> int:
    """Parse ``argv`` and run the matching command.

    Args:
        argv: argument list **without** the program name.
        transport: the transport to execute against.
        as_json: emit the JSON envelope (False ⇒ human summary).
        stream: output stream.

    Returns:
        A process exit code; never raises for a user error (usage ⇒
        ``EXIT_USAGE``).
    """
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:  # argparse already printed help/usage
        return int(exc.code or EXIT_USAGE)
    handler = getattr(args, "handler", None)
    if handler is None:
        return run_repl(transport, stream=stream)
    try:
        payload = handler(transport, args)
    except FeatureDisabled as exc:
        payload = error_envelope("FEATURE_DISABLED", str(exc), flag=exc.flag)
    except Exception as exc:  # a CLI never crashes an agent's tool loop
        payload = error_envelope(type(exc).__name__, str(exc))
    return emit(payload, as_json=as_json and not getattr(args, "pretty", False), stream=stream)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point.

    Args:
        argv: argument list (defaults to ``sys.argv[1:]``).

    Returns:
        The process exit code.
    """
    raw = list(sys.argv[1:] if argv is None else argv)
    # Global transport flags may appear anywhere in argv; pull them out first so
    # the subcommand parser never has to know about them.
    globals_parser = argparse.ArgumentParser(add_help=False)
    globals_parser.add_argument("--base-url", default="")
    globals_parser.add_argument("--token", default="")
    known, rest = globals_parser.parse_known_args(raw)
    transport = Transport(base_url=known.base_url, token=known.token)
    return dispatch(rest, transport)


if __name__ == "__main__":
    sys.exit(main())


CommandFn = Callable[[Transport, argparse.Namespace], dict[str, Any]]
