#!/usr/bin/env python3
"""Send one turn to a veya bot or group from the shell (Codync CLI-turn method).

The turn runs through the same async path the web UI uses (``?async=1``),
so a turn that takes minutes does not hold a request open. This script
waits for the reply to land in the transcript, then prints it.

Usage:
    veya-send --bot chef "what is for dinner?"
    veya-send --bot chef --wait 60 --json "what is for dinner?"
    veya-send --group grp_abc "ideas?" --no-wait
    echo "hello" | veya-send --bot chef          # text from stdin

Authentication: a bearer token from ``$VEYA_API_TOKEN``, ``--token``,
or ``--token-file`` (default ``~/.veya/api-token``). The token is the
owner token the operator mints; this script never asks for a password.
Base URL: ``$VEYA_BASE_URL`` or ``--base-url`` (default
``http://127.0.0.1:8767``, the host-local backend).

Exit codes (Codync ``send`` grades, adapted):
    0  the reply landed in the transcript
    1  usage error or an HTTP error from the server (auth, unknown bot, ...)
    2  timed out waiting for the reply
    3  timed out, but a new open attention item appeared for this
       conversation while waiting: the turn is paused on the owner
    4  transport error (cannot reach the server at all)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

DEFAULT_BASE_URL = "http://127.0.0.1:8767"
DEFAULT_WAIT_S = 300.0
DEFAULT_POLL_S = 2.0


class UsageError(Exception):
    """Exit 1: the invocation or the server's answer is wrong."""


class TransportError(Exception):
    """Exit 4: the server could not be reached at all."""


def _read_token(args: argparse.Namespace) -> str:
    if args.token:
        return args.token
    env_token = os.environ.get("VEYA_API_TOKEN", "").strip()
    if env_token:
        return env_token
    token_file = Path(args.token_file).expanduser()
    try:
        token = token_file.read_text(encoding="utf-8").strip()
    except OSError:
        token = ""
    if token:
        return token.splitlines()[0].strip()
    raise UsageError(
        "no API token: set VEYA_API_TOKEN, pass --token, or put the token in "
        f"{token_file} (one line, mode 0600)"
    )


def _request(
    method: str, url: str, token: str, payload: dict[str, Any] | None = None
) -> Any:
    data = None
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        raise UsageError(f"server answered {exc.code}: {detail or exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise TransportError(f"cannot reach {url}: {exc.reason}") from exc
    try:
        return json.loads(body) if body.strip() else {}
    except ValueError as exc:
        raise UsageError(f"server answered non-JSON: {body[:200]!r}") from exc


def _messages(base_url: str, token: str, kind: str, ref: str) -> list[dict[str, Any]]:
    if kind == "bot":
        path = f"/api/v1/bots/{ref}/messages"
    else:
        path = f"/api/v1/bots/groups/{ref}/messages"
    payload = _request("GET", base_url + path, token)
    messages = payload.get("messages", []) if isinstance(payload, dict) else []
    return [m for m in messages if isinstance(m, dict)]


def _new_attention(
    base_url: str, token: str, kind: str, ref: str, since_ts: float
) -> bool:
    """True when a new open attention item appeared for this conversation."""

    try:
        payload = _request("GET", base_url + "/api/v1/bots/attention?status=open", token)
    except (UsageError, TransportError):
        return False
    items = payload.get("attention", []) if isinstance(payload, dict) else []
    for item in items:
        if not isinstance(item, dict) or item.get("status") != "open":
            continue
        created = item.get("created_ts")
        if isinstance(created, (int, float)) and created < since_ts - 1:
            continue
        if kind == "bot" and item.get("bot_id") == ref:
            return True
        if kind == "group" and item.get("group_id") == ref:
            return True
    return False


def send_turn(args: argparse.Namespace) -> int:
    token = _read_token(args)
    base_url = args.base_url.rstrip("/")
    kind = "group" if args.group else "bot"
    ref = args.group or args.bot or ""

    text = args.text
    if text is None:
        text = sys.stdin.read()
    text = (text or "").strip()
    if not text:
        raise UsageError("no message text: pass it as an argument or on stdin")

    if kind == "bot":
        path = f"/api/v1/bots/{ref}/messages?async=1"
    else:
        path = f"/api/v1/bots/groups/{ref}/messages?async=1"
    started_ts = time.time()
    before = _messages(base_url, token, kind, ref) if kind == "group" else []
    before_ids = {str(m.get("id")) for m in before}
    opened = _request("POST", base_url + path, token, {"text": text})
    task_id = str(opened.get("task_id") or "") if isinstance(opened, dict) else ""
    if args.no_wait:
        print(json.dumps({"task_id": task_id, kind + "_id": ref}, ensure_ascii=False))
        return 0

    deadline = started_ts + args.wait
    while True:
        messages = _messages(base_url, token, kind, ref)
        if kind == "bot" and task_id:
            for message in messages:
                if message.get("role") == "bot" and str(message.get("task_id")) == task_id:
                    return _emit(args, ref, task_id, [message])
        else:
            fresh = [
                m
                for m in messages
                if m.get("role") == "bot" and str(m.get("id")) not in before_ids
            ]
            if fresh:
                return _emit(args, ref, task_id, fresh)
        if time.time() >= deadline:
            if _new_attention(base_url, token, kind, ref, started_ts):
                print("turn is waiting on the owner (open attention item)", file=sys.stderr)
                return 3
            print("timed out waiting for the reply", file=sys.stderr)
            return 2
        time.sleep(min(args.poll, max(0.05, deadline - time.time())))


def _emit(
    args: argparse.Namespace, ref: str, task_id: str, replies: list[dict[str, Any]]
) -> int:
    if args.json:
        print(
            json.dumps(
                {
                    "task_id": task_id,
                    "replies": [
                        {
                            "bot_id": r.get("bot_id"),
                            "reply": r.get("text", ""),
                            "task_id": r.get("task_id"),
                        }
                        for r in replies
                    ],
                },
                ensure_ascii=False,
            )
        )
    else:
        for reply in replies:
            print(reply.get("text", ""))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Send one turn to a veya bot or group and wait for the reply."
    )
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--bot", help="bot id to message")
    target.add_argument("--group", help="group id to message")
    parser.add_argument("text", nargs="?", help="message text (or stdin when omitted)")
    parser.add_argument("--base-url", default=os.environ.get("VEYA_BASE_URL", DEFAULT_BASE_URL))
    parser.add_argument("--token", default="", help="bearer token (prefer env/file)")
    parser.add_argument(
        "--token-file",
        default="~/.veya/api-token",
        help="file holding the bearer token on one line",
    )
    parser.add_argument("--wait", type=float, default=DEFAULT_WAIT_S, help="seconds to wait (default 300)")
    parser.add_argument("--poll", type=float, default=DEFAULT_POLL_S, help="poll interval in seconds")
    parser.add_argument("--no-wait", action="store_true", help="open the turn and return at once")
    parser.add_argument("--json", action="store_true", help="print the reply envelope as JSON")
    args = parser.parse_args(argv)
    try:
        return send_turn(args)
    except UsageError as exc:
        print(f"veya-send: {exc}", file=sys.stderr)
        return 1
    except TransportError as exc:
        print(f"veya-send: {exc}", file=sys.stderr)
        return 4


if __name__ == "__main__":
    raise SystemExit(main())
