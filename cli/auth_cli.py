"""``veya auth token`` — mint and verify API tokens for CLI/HTTP automation.

Tokens are minted by the existing :mod:`server.auth` authority
(``authenticate`` → ``issue_token``), so they are the same bearer tokens the
HTTP ``AuthGateMiddleware`` already accepts.  Nothing here weakens auth: a
mint requires a valid username/password (or an existing token), and the CLI
never prints a password or writes the token anywhere but stdout.

Usage:
    veya auth token --username alice            # password from $VEYA_AUTH_PASSWORD
    veya auth token --verify <token>            # print the resolved user
    veya auth token --revoke <token>
    veya auth whoami                            # token from $VEYA_API_TOKEN
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

ENV_PASSWORD = "VEYA_AUTH_PASSWORD"
ENV_TOKEN = "VEYA_API_TOKEN"


def _emit(payload: dict[str, Any], *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False))
        return
    if payload.get("status") == "ok" and payload.get("token"):
        print(payload["token"])
        return
    if payload.get("status") == "ok":
        print(f"{payload.get('username')} ({payload.get('user_id')})")
        return
    print(f"error: {payload.get('error')}", file=sys.stderr)


def _mint(username: str, password: str, *, as_json: bool) -> int:
    from server import auth as auth_mod

    user = auth_mod.authenticate(username, password)
    if user is None:
        _emit({"status": "failed", "error": "invalid username or password"}, as_json=as_json)
        return 1
    token = auth_mod.issue_token(user["user_id"])
    _emit(
        {
            "status": "ok",
            "token": token,
            "user_id": user["user_id"],
            "username": user["username"],
            "is_owner": bool(user["is_owner"]),
        },
        as_json=as_json,
    )
    return 0


def _verify(token: str, *, as_json: bool) -> int:
    from server import auth as auth_mod

    user = auth_mod.resolve_token(token)
    if user is None:
        _emit({"status": "failed", "error": "token is invalid or expired"}, as_json=as_json)
        return 1
    _emit(
        {
            "status": "ok",
            "user_id": user["user_id"],
            "username": user["username"],
            "is_owner": bool(user.get("is_owner")),
        },
        as_json=as_json,
    )
    return 0


def _revoke(token: str, *, as_json: bool) -> int:
    from server import auth as auth_mod

    if auth_mod.resolve_token(token) is None:
        _emit({"status": "failed", "error": "token is invalid or expired"}, as_json=as_json)
        return 1
    auth_mod.revoke_token(token)
    _emit({"status": "ok", "revoked": True}, as_json=as_json)
    return 0


def run_auth_cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="veya auth", description="API token helper")
    sub = parser.add_subparsers(dest="command")

    token = sub.add_parser("token", help="mint, verify, or revoke an API token")
    token.add_argument("--username", help="account username (mint)")
    token.add_argument(
        "--password",
        help=f"account password (prefer ${ENV_PASSWORD} to keep it out of shell history)",
    )
    token.add_argument("--verify", metavar="TOKEN", help="resolve a token to its user")
    token.add_argument("--revoke", metavar="TOKEN", help="revoke a token")
    token.add_argument("--json", action="store_true", help="machine-readable output")

    whoami = sub.add_parser("whoami", help=f"verify the token in ${ENV_TOKEN}")
    whoami.add_argument("--json", action="store_true", help="machine-readable output")

    args = parser.parse_args(argv)
    if args.command == "whoami":
        token_value = os.environ.get(ENV_TOKEN, "").strip()
        if not token_value:
            _emit(
                {"status": "failed", "error": f"no token in ${ENV_TOKEN}"},
                as_json=args.json,
            )
            return 2
        return _verify(token_value, as_json=args.json)
    if args.command != "token":
        parser.print_help(sys.stderr)
        return 2
    if args.verify:
        return _verify(args.verify, as_json=args.json)
    if args.revoke:
        return _revoke(args.revoke, as_json=args.json)
    password = args.password or os.environ.get(ENV_PASSWORD, "")
    if not args.username or not password:
        _emit(
            {
                "status": "failed",
                "error": f"--username and password (or ${ENV_PASSWORD}) are required",
            },
            as_json=args.json,
        )
        return 2
    return _mint(args.username, password, as_json=args.json)


__all__ = ["ENV_PASSWORD", "ENV_TOKEN", "run_auth_cli"]
