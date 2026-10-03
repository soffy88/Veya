"""Operator CLI for remote MCP bearer tokens (spec §6).

Usage::

    python -m veya.remote issue --principal alice --workspace /srv/repo \
        --write --shell --git --ttl 86400
    python -m veya.remote list
    python -m veya.remote rotate --token-id rt_ab12...
    python -m veya.remote revoke --token-id rt_ab12...

The raw secret is printed exactly once (on ``issue``/``rotate``) and never
stored; only its SHA-256 digest is persisted in the token store.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from dataclasses import asdict
from typing import cast

from .auth import RemoteAuth
from .models import RemotePermissions


def _permissions(args: argparse.Namespace) -> RemotePermissions:
    return RemotePermissions(
        read=True,
        write=bool(args.write),
        shell=bool(args.shell),
        git=bool(args.git),
        network=bool(args.network),
        destructive=bool(args.destructive),
        service_control=bool(args.service_control),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="veya.remote", description="Remote MCP token admin")
    sub = parser.add_subparsers(dest="command", required=True)

    issue = sub.add_parser("issue", help="create a token (prints the secret once)")
    issue.add_argument("--principal", required=True)
    issue.add_argument("--workspace", action="append", default=[], help="repeatable")
    issue.add_argument("--write", action="store_true")
    issue.add_argument("--shell", action="store_true")
    issue.add_argument("--git", action="store_true")
    issue.add_argument("--network", action="store_true")
    issue.add_argument("--destructive", action="store_true")
    issue.add_argument("--service-control", action="store_true")
    issue.add_argument("--ttl", type=float, default=None, help="seconds until expiry")
    issue.add_argument("--label", default="")

    listing = sub.add_parser("list", help="list tokens (never prints secrets)")
    listing.add_argument("--json", action="store_true")

    revoke = sub.add_parser("revoke", help="revoke a token")
    revoke.add_argument("--token-id", required=True)

    rotate = sub.add_parser("rotate", help="rotate a token secret (prints the new secret once)")
    rotate.add_argument("--token-id", required=True)

    serve = sub.add_parser("serve", help="run the Remote MCP HTTP gateway (loopback only)")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8790)
    serve.add_argument("--log-level", default="info")

    exec_p = sub.add_parser("execution", help="execution lifecycle operations")
    exec_sub = exec_p.add_subparsers(dest="exec_command", required=True)

    # execution list
    ex_list = exec_sub.add_parser("list", help="list executions")
    ex_list.add_argument("--principal", default=None)
    ex_list.add_argument("--token-id", default=None)
    ex_list.add_argument("--json", action="store_true")

    # execution status
    ex_status = exec_sub.add_parser("status", help="get execution status")
    ex_status.add_argument("--execution-id", required=True)
    ex_status.add_argument("--token-id", default=None)
    ex_status.add_argument("--principal", default=None)
    ex_status.add_argument("--json", action="store_true")

    # execution suspend
    ex_suspend = exec_sub.add_parser("suspend", help="suspend execution")
    ex_suspend.add_argument("--execution-id", required=True)
    ex_suspend.add_argument("--token-id", default=None)
    ex_suspend.add_argument("--principal", default=None)
    ex_suspend.add_argument("--json", action="store_true")

    # execution resume
    ex_resume = exec_sub.add_parser("resume", help="resume execution")
    ex_resume.add_argument("--execution-id", required=True)
    ex_resume.add_argument("--token-id", default=None)
    ex_resume.add_argument("--principal", default=None)
    ex_resume.add_argument("--json", action="store_true")

    # execution cancel
    ex_cancel = exec_sub.add_parser("cancel", help="cancel execution")
    ex_cancel.add_argument("--execution-id", required=True)
    ex_cancel.add_argument("--token-id", default=None)
    ex_cancel.add_argument("--principal", default=None)
    ex_cancel.add_argument("--json", action="store_true")

    # execution manifest
    ex_manifest = exec_sub.add_parser("manifest", help="probe executor capability manifests")
    ex_manifest.add_argument(
        "--executor",
        choices=["HICODE", "DSH", "PI", "GROK", "CODEX", "ALL"],
        default="ALL",
    )
    ex_manifest.add_argument("--json", action="store_true")

    # execution events
    ex_events = exec_sub.add_parser("events", help="list execution events")
    ex_events.add_argument("--execution-id", default=None)
    ex_events.add_argument("--limit", type=int, default=50)
    ex_events.add_argument("--json", action="store_true")

    return parser


def _serve(host: str, port: int, log_level: str) -> int:
    """Run the Remote MCP router as a loopback-only ASGI service."""

    if host not in {"127.0.0.1", "localhost", "::1"}:
        print(
            f"error: refusing to bind non-loopback host {host!r} "
            "(expose via a reverse proxy instead)",
            file=sys.stderr,
        )
        return 2

    try:
        from veya import platform as veya_platform

        if veya_platform.available("obase"):
            veya_platform.load("obase")
    except Exception:
        pass

    import uvicorn
    from fastapi import FastAPI

    from server.routes.remote_mcp import router
    from veya.remote.oauth import router as oauth_router

    app = FastAPI(title="veya-remote-mcp", docs_url=None, redoc_url=None, openapi_url=None)
    app.include_router(router)
    app.include_router(oauth_router)
    print(f"veya-remote-mcp listening on http://{host}:{port} (loopback only)", file=sys.stderr)
    uvicorn.run(app, host=host, port=port, log_level=log_level)
    return 0


def _handle_execution(args: argparse.Namespace) -> int:
    from .execution import DurableJobManager, ExecutionStore
    from .execution_contract import probe_runtime_capability_manifest

    store = ExecutionStore.from_env(default_persistent=True)
    manager = DurableJobManager(store=store)

    if args.exec_command == "manifest":
        executors = (
            ["HICODE", "DSH", "PI", "GROK", "CODEX"] if args.executor == "ALL" else [args.executor]
        )
        manifests = {e: probe_runtime_capability_manifest(e).to_dict() for e in executors}
        if args.json:
            print(
                json.dumps(
                    manifests if args.executor == "ALL" else manifests[args.executor],
                    indent=2,
                )
            )
        else:
            for e, m in manifests.items():
                print(
                    f"{e:<10} status={m['status']:<12} streaming={m['supports_streaming']} "
                    f"cancel={m['supports_cancel']} suspend={m['supports_suspend']}"
                )
        return 0

    if args.exec_command == "list":
        records = list(store.load_all())
        if args.principal:
            records = [
                r
                for r in records
                if getattr(r, "principal_id", None) == args.principal
                or getattr(r, "principal", None) == args.principal
            ]
        if args.token_id:
            records = [r for r in records if r.token_id == args.token_id]

        rows = [
            {
                "execution_id": r.execution_id,
                "status": r.status,
                "phase": str(r.phase),
                "principal_id": getattr(r, "principal_id", "") or getattr(r, "principal", ""),
                "token_id": r.token_id,
                "session_id": r.session_id,
                "isolated_worktree": getattr(r, "isolated_worktree", False),
                "created_at": r.created_at,
            }
            for r in records
        ]
        if args.json:
            print(json.dumps(rows, indent=2))
        else:
            for row in rows:
                print(
                    f"{row['execution_id']}  {row['status']:<12} {row['phase']:<12} "
                    f"{row['principal_id']:<16} {row['session_id']}"
                )
        return 0

    if args.exec_command == "status":
        record = manager.lookup(args.execution_id)
        if record is None:
            err_msg = {"error": "NOT_FOUND", "message": "unknown execution_id"}
            print(
                json.dumps(err_msg)
                if args.json
                else f"error: unknown execution_id {args.execution_id}",
                file=sys.stderr,
            )
            return 1
        tok = args.token_id or record.token_id
        try:
            rec = manager.status(args.execution_id, token_id=tok, principal=args.principal)
            data = asdict(rec)
            if args.json:
                print(json.dumps(data, indent=2, default=str))
            else:
                print(
                    f"{rec.execution_id}  status={rec.status} phase={rec.phase} "
                    f"principal={getattr(rec, 'principal_id', rec.principal)}"
                )
            return 0
        except Exception as exc:
            err_msg = {"error": getattr(exc, "code", type(exc).__name__), "message": str(exc)}
            print(json.dumps(err_msg) if args.json else f"error: {exc}", file=sys.stderr)
            return 1

    if args.exec_command == "suspend":
        record = manager.lookup(args.execution_id)
        if record is None:
            err_msg = {"error": "NOT_FOUND", "message": "unknown execution_id"}
            print(
                json.dumps(err_msg)
                if args.json
                else f"error: unknown execution_id {args.execution_id}",
                file=sys.stderr,
            )
            return 1
        tok = args.token_id or record.token_id
        try:
            import asyncio

            rec = asyncio.run(
                manager.suspend(args.execution_id, token_id=tok, principal=args.principal)
            )
            data = asdict(rec)
            if args.json:
                print(json.dumps(data, indent=2, default=str))
            else:
                print(f"suspended {rec.execution_id} phase={rec.phase}")
            return 0
        except Exception as exc:
            err_msg = {"error": getattr(exc, "code", type(exc).__name__), "message": str(exc)}
            print(json.dumps(err_msg) if args.json else f"error: {exc}", file=sys.stderr)
            return 1

    if args.exec_command == "resume":
        record = manager.lookup(args.execution_id)
        if record is None:
            err_msg = {"error": "NOT_FOUND", "message": "unknown execution_id"}
            print(
                json.dumps(err_msg)
                if args.json
                else f"error: unknown execution_id {args.execution_id}",
                file=sys.stderr,
            )
            return 1
        tok = args.token_id or record.token_id
        try:
            import asyncio

            rec = asyncio.run(
                manager.resume(args.execution_id, token_id=tok, principal=args.principal)
            )
            data = asdict(rec)
            if args.json:
                print(json.dumps(data, indent=2, default=str))
            else:
                print(f"resumed {rec.execution_id} phase={rec.phase}")
            return 0
        except Exception as exc:
            err_msg = {"error": getattr(exc, "code", type(exc).__name__), "message": str(exc)}
            print(json.dumps(err_msg) if args.json else f"error: {exc}", file=sys.stderr)
            return 1

    if args.exec_command == "cancel":
        record = manager.lookup(args.execution_id)
        if record is None:
            err_msg = {"error": "NOT_FOUND", "message": "unknown execution_id"}
            print(
                json.dumps(err_msg)
                if args.json
                else f"error: unknown execution_id {args.execution_id}",
                file=sys.stderr,
            )
            return 1
        tok = args.token_id or record.token_id
        try:
            import asyncio

            rec = asyncio.run(
                manager.cancel(args.execution_id, token_id=tok, principal=args.principal)
            )
            data = asdict(rec)
            if args.json:
                print(json.dumps(data, indent=2, default=str))
            else:
                print(f"cancelled {rec.execution_id} status={rec.status}")
            return 0
        except Exception as exc:
            err_msg = {"error": getattr(exc, "code", type(exc).__name__), "message": str(exc)}
            print(json.dumps(err_msg) if args.json else f"error: {exc}", file=sys.stderr)
            return 1

    if args.exec_command == "events":
        if args.execution_id:
            record = manager.lookup(args.execution_id)
            if record is None:
                err_msg = {"error": "NOT_FOUND", "message": "unknown execution_id"}
                print(
                    json.dumps(err_msg)
                    if args.json
                    else f"error: unknown execution_id {args.execution_id}",
                    file=sys.stderr,
                )
                return 1
            events = record.events[-args.limit :] if args.limit else record.events
        else:
            all_events = []
            for r in store.load_all():
                all_events.extend(r.events)
            all_events.sort(key=lambda ev: ev.get("ts", 0))
            events = all_events[-args.limit :] if args.limit else all_events

        if args.json:
            print(json.dumps(events, indent=2, default=str))
        else:
            for ev in events:
                print(f"[{ev.get('ts')}] {ev.get('kind', 'event')} - {ev.get('message', '')}")
        return 0

    return 2


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.command == "serve":
        return _serve(args.host, args.port, args.log_level)

    if args.command == "execution":
        return _handle_execution(args)

    auth = RemoteAuth.from_env()

    if args.command == "issue":
        if not args.workspace:
            print("error: at least one --workspace is required (fail closed)", file=sys.stderr)
            return 2
        record, secret = auth.issue(
            args.principal,
            permissions=_permissions(args),
            workspaces=args.workspace,
            ttl_s=args.ttl,
            label=args.label,
        )
        print(
            json.dumps(
                {"token_id": record.token_id, "secret": secret, "principal": record.principal}
            )
        )
        return 0

    if args.command == "list":
        rows = [
            {
                "token_id": token.token_id,
                "principal": token.principal,
                "permissions": token.permissions.to_dict(),
                "workspaces": list(token.workspaces),
                "active": token.is_active(),
            }
            for token in auth.tokens()
        ]
        if args.json:
            print(json.dumps(rows, indent=2))
        else:
            for row in rows:
                state = "active" if row["active"] else "revoked/expired"
                print(
                    f"{row['token_id']}  {row['principal']:<16} {state:<15} {','.join(cast(list[str], row['workspaces']))}"
                )
        return 0

    if args.command == "revoke":
        if not auth.revoke(args.token_id):
            print(f"error: unknown token {args.token_id}", file=sys.stderr)
            return 1
        print(f"revoked {args.token_id}")
        return 0

    if args.command == "rotate":
        rotated_secret = auth.rotate(args.token_id)
        if rotated_secret is None:
            print(f"error: unknown token {args.token_id}", file=sys.stderr)
            return 1
        print(json.dumps({"token_id": args.token_id, "secret": rotated_secret}))
        return 0

    return 2  # pragma: no cover - argparse enforces a valid command


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
