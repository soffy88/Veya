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

    app = FastAPI(title="veya-remote-mcp", docs_url=None, redoc_url=None, openapi_url=None)
    app.include_router(router)
    print(f"veya-remote-mcp listening on http://{host}:{port} (loopback only)", file=sys.stderr)
    uvicorn.run(app, host=host, port=port, log_level=log_level)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.command == "serve":
        return _serve(args.host, args.port, args.log_level)

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
                    f"{row['token_id']}  {row['principal']:<16} {state:<15} {','.join(row['workspaces'])}"
                )
        return 0

    if args.command == "revoke":
        if not auth.revoke(args.token_id):
            print(f"error: unknown token {args.token_id}", file=sys.stderr)
            return 1
        print(f"revoked {args.token_id}")
        return 0

    if args.command == "rotate":
        secret = auth.rotate(args.token_id)
        if secret is None:
            print(f"error: unknown token {args.token_id}", file=sys.stderr)
            return 1
        print(json.dumps({"token_id": args.token_id, "secret": secret}))
        return 0

    return 2  # pragma: no cover - argparse enforces a valid command


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
